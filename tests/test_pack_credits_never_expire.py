#!/usr/bin/env python3
"""Pack credits never expire (2026-09-11).

/pricing has promised it since 2026-06-17 — in the pack Offer JSON-LD, the price
note and the FAQ — while grant_credit_pack() wrote expires_at = NOW() + 90 days
(DCHUB_PACK{5,10}_EXPIRY_DAYS, default 90). The first pack sold (r-pack5 shipped
2026-06-16) would have lapsed about 2026-09-14. Owner decision 2026-09-10: the
code follows the page.

NO NETWORK, NO DB in this file: the connection is faked and the SQL captured,
the harness shape of tests/test_pack10_records_its_own_price.py. What only a
real Postgres can prove — that the backfill moves exactly the pack rows, is
idempotent, runs at import, and that both drivers load the instant back — is in
tests/test_pack_credits_never_expire_sql.py, which the db-parity job runs.
"""
import ast
import datetime as dt
import inspect
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import routes.mcp_conversion_plays as mcp  # noqa: E402


class _Cur:
    """Shaped like the real psycopg2 cursor this code uses. Nothing more capable."""
    def __init__(self, rows=None):
        self.calls, self._n, self._rows = [], 0, rows or []

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

    def fetchall(self):
        return list(self._rows)


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


def _insert(monkeypatch, source, **kw):
    cur = _Cur()
    monkeypatch.setattr(mcp, "_conn", lambda: _Conn(cur))
    out = mcp.grant_credit_pack("dch_live_testkey", "sess-1", 1000,
                                stripe_session_id="cs_test_1", source=source, **kw)
    assert out.get("ok"), out
    inserts = [c for c in cur.calls if "INSERT INTO mcp_topups" in c[0]]
    assert len(inserts) == 1, f"expected one INSERT, got {len(inserts)}"
    return inserts[0]


def _column(sql, params, column):
    """(value expression, bound parameter) for ONE column of the INSERT, derived
    from the column list and the VALUES clause — never from an index, so a
    reordered INSERT cannot move the assertion onto a different column."""
    cols = [c.strip() for c in
            re.search(r"INSERT INTO mcp_topups\s*\(([^)]*)\)", sql, re.S).group(1).split(",")]
    vals = re.search(r"VALUES\s*\((.*?)\)\s*RETURNING", sql, re.S).group(1)
    parts, depth, cur = [], 0, ""
    for ch in vals:
        depth += ch == "("
        depth -= ch == ")"
        if ch == "," and depth == 0:
            parts.append(cur); cur = ""
        else:
            cur += ch
    parts.append(cur)
    assert len(parts) == len(cols), f"{len(cols)} columns but {len(parts)} value expressions"
    seen = 0
    for col, expr in zip(cols, parts):
        n = expr.count("%s")
        if col == column:
            assert n <= 1, f"{column} is bound to {n} placeholders"
            return expr.strip(), (params[seen] if n else None)
        seen += n
    raise AssertionError(f"no {column} column in the INSERT")


def _py_files_mentioning(token):
    skip = {"venv", ".venv", "node_modules", ".git", "tests"}
    for p in ROOT.rglob("*.py"):
        if skip & set(p.relative_to(ROOT).parts):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if token in text:
            yield p, text


def _grant_calls():
    """(relpath, module tree, Call) for every call to grant_credit_pack, under any
    import alias, in app code. FLOORED: the three known call sites must be found,
    or the scan itself is broken and every assertion over it passes on nothing."""
    found = []
    for path, text in _py_files_mentioning("grant_credit_pack"):
        tree = ast.parse(text)
        names = {"grant_credit_pack"} | {
            a.asname for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
            for a in n.names if a.name == "grant_credit_pack" and a.asname}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
                if name in names:
                    found.append((path.relative_to(ROOT).as_posix(), tree, node))
    where = {p for p, _t, _c in found}
    assert len(found) >= 3 and {"main.py", "routes/stripe_metered.py"} <= where, \
        f"the call-site scan found {[(p, c.lineno) for p, _t, c in found]} — it is broken, not clean"
    return found


# ── every grant is written never to expire ────────────────────────────────────

@pytest.mark.parametrize("source", mcp.PACK_SOURCES)
def test_every_pack_source_is_written_never_to_expire(monkeypatch, source):
    sql, params = _insert(monkeypatch, source)
    expr, value = _column(sql, params, "expires_at")
    assert value == mcp.PACK_NEVER_EXPIRES, (expr, value)
    assert "interval" not in expr.lower() and "now()" not in expr.lower(), expr


def test_pack_sources_is_exactly_what_the_callers_write():
    """PACK_SOURCES scopes the backfill. A source a caller writes but the tuple
    omits keeps its 90-day clock; a stale entry names rows nobody writes. Each
    call's `source=` is resolved from the AST — literal, conditional, or the
    variable it was assigned from — so the tuple is checked against the real
    call sites, not against a copy of itself."""
    default = inspect.signature(mcp.grant_credit_pack).parameters["source"].default
    written = set()
    for path, tree, call in _grant_calls():
        kw = next((k for k in call.keywords if k.arg == "source"), None)
        if kw is None:
            written.add(default)
            continue
        strings = lambda node: {n.value for n in ast.walk(node)
                                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        consts = strings(kw.value)
        names = {n.id for n in ast.walk(kw.value) if isinstance(n, ast.Name)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id in names for t in node.targets):
                consts |= strings(node.value)
        assert consts, f"{path}:{call.lineno} source= resolved to no literal"
        written |= consts
    assert written == set(mcp.PACK_SOURCES), \
        f"callers write {sorted(written)}; PACK_SOURCES is {sorted(mcp.PACK_SOURCES)}"


def test_never_is_a_far_future_utc_instant():
    when = dt.datetime.fromisoformat(mcp.PACK_NEVER_EXPIRES.replace(" ", "T"))
    assert when.utcoffset() == dt.timedelta(0)
    assert when.year == 9999, mcp.PACK_NEVER_EXPIRES


# ── nothing is left that can shorten it ───────────────────────────────────────

def test_there_is_no_expiry_argument_left():
    assert "expires_days" not in inspect.signature(mcp.grant_credit_pack).parameters
    with pytest.raises(TypeError):
        mcp.grant_credit_pack("k", None, 1000, expires_days=90)


def test_no_env_var_can_rearm_the_clock():
    """AST, not text: the module's own comment names the retired variables."""
    tree = ast.parse((ROOT / "routes/mcp_conversion_plays.py").read_text(encoding="utf-8"))
    env_reads = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and n.args
                 and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)
                 and "EXPIR" in n.args[0].value.upper()
                 and ast.unparse(n.func) in ("os.environ.get", "os.getenv")]
    assert env_reads == [], [ast.unparse(n) for n in env_reads]
    assert not hasattr(mcp, "PACK5_EXPIRY_DAYS") and not hasattr(mcp, "PACK10_EXPIRY_DAYS")


def test_no_caller_passes_or_imports_an_expiry():
    passing = [(p, c.lineno) for p, _t, c in _grant_calls()
               if any(k.arg == "expires_days" for k in c.keywords)]
    assert passing == [], f"a caller still passes an expiry: {passing}"
    bad_imports = []
    for path, text in _py_files_mentioning("EXPIRY_DAYS"):
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom):
                bad_imports += [(path.name, node.lineno, a.name) for a in node.names
                                if re.fullmatch(r"PACK\d+_EXPIRY_DAYS", a.name)]
    assert bad_imports == [], f"a retired expiry knob is still imported: {bad_imports}"


# ── the backfill, shape only (the SQL file proves the behaviour) ─────────────

def test_the_backfill_targets_pack_rows_and_binds_never(monkeypatch, capsys):
    old = [dt.datetime(2026, 9, 14, 3, 0, tzinfo=dt.timezone.utc),
           dt.datetime(2026, 12, 8, 17, 30, tzinfo=dt.timezone.utc)]
    cur = _Cur(rows=[(11, old[1]), (7, old[0])])       # (id, prior expiry), as RETURNING yields
    monkeypatch.setattr(mcp, "_conn", lambda: _Conn(cur))
    out = mcp.restore_pack_never_expires()
    assert out == {"ok": True, "restored": 2,
                   "earliest_old_expiry": old[0].isoformat(),
                   "latest_old_expiry": old[1].isoformat()}, out
    # the audit trail: each id with the expiry it held, enough to reverse it
    err = capsys.readouterr().err
    assert f"id=7:{old[0].isoformat()}" in err and f"id=11:{old[1].isoformat()}" in err, err
    assert len(cur.calls) == 1
    sql, params = cur.calls[0]
    pieces = " ".join(sql.split()).split("%s")
    assert len(pieces) - 1 == len(params) == 3, (pieces, params)
    # each placeholder, bound to the SQL immediately before it
    before = [pieces[i].rstrip() for i in range(3)]
    assert before[0].endswith("FROM mcp_topups WHERE source = ANY(") \
        and params[0] == list(mcp.PACK_SOURCES), (before[0], params[0])
    assert before[1].endswith("AND expires_at <") and params[1] == mcp.PACK_NEVER_EXPIRES, (before[1], params[1])
    assert before[2].endswith("SET expires_at =") and params[2] == mcp.PACK_NEVER_EXPIRES, (before[2], params[2])


def test_the_backfill_never_raises(monkeypatch):
    monkeypatch.setattr(mcp, "_conn", lambda: None)
    assert mcp.restore_pack_never_expires() == {"ok": False, "restored": 0, "error": "no_database"}

    class _Boom(_Cur):
        def execute(self, sql, params=None):
            raise RuntimeError("relation mcp_topups is locked")
    monkeypatch.setattr(mcp, "_conn", lambda: _Conn(_Boom()))
    out = mcp.restore_pack_never_expires()
    assert out["ok"] is False and "locked" in out["error"]
