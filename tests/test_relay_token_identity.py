"""An open with no session_id still has an identity: the token it was opened with.

Measured 2026-09-07 over 30d: 178 relay opens, 32 passed the real-UA filter,
and 30 of those 32 carried NO session_id — so human_acted could count 2. The
sid is baked in at MINT time (`${sessionId || ''}|tool|tier|ts` in
buildHumanRelay), so a link minted without a session is born without one and no
read-side change recovers it.

Every link does carry an HMAC token unique per mint. Hashing it on the open
gives those rows an identity, and the counted unit becomes DISTINCT LINKS
OPENED rather than distinct sessions.
"""
import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
RELAY = ROOT / "routes" / "human_relay.py"
FUNNEL = ROOT / "flask_mcp_endpoints.py"


def _log_open_fn():
    tree = ast.parse(RELAY.read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_log_open"), None)
    assert fn is not None, "_log_open not found"
    return fn


def test_the_hash_is_derived_from_the_token_argument():
    """★ Bind the VALUE. A column named token_hash that is written NULL, or a
    constant, looks identical in the schema and publishes nothing."""
    fn = _log_open_fn()
    assert "token" in {a.arg for a in fn.args.args}, "_log_open lost its token arg"
    found = False
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "hexdigest"):
            src = ast.dump(node)
            # walk up is awkward; check the enclosing expression text instead
            found = True
    assert found, "no hexdigest() call in _log_open — nothing hashes the token"
    seg = RELAY.read_text(encoding="utf-8")
    i = seg.index("token_hash")
    tail = seg[i:i + 4000]
    assert re.search(r"hashlib\.sha256\(\(token or \"\"\)\.encode\(\)\)", tail), (
        "token_hash is not sha256 of the token argument")


def test_the_raw_token_is_never_stored():
    """★ The token is a WORKING CREDENTIAL for /upgrade/h/<token>. This table
    is a telemetry log and has no business holding one."""
    # ★ AST, not regex. The first version of this test grepped the INSERT text
    # for the word "token" and failed on `hashlib.sha256((token or "")...)` —
    # which is the whole point of the change. What matters is whether a BARE
    # `token` is passed as a value, so bind the value tuple.
    fn = _log_open_fn()
    src = RELAY.read_text(encoding="utf-8")
    assert "token_hash" in src[src.index("INSERT INTO relay_opens"):
                               src.index("conn.commit()")], (
        "the insert does not carry token_hash")
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute" and len(node.args) >= 2):
            sql = node.args[0]
            text = sql.value if isinstance(sql, ast.Constant) else ast.dump(sql)
            if "INSERT INTO relay_opens" not in str(text):
                continue
            for el in ast.walk(node.args[1]):
                if isinstance(el, ast.Name) and el.id == "token":
                    # legal only INSIDE a hashing call
                    enclosing = [n for n in ast.walk(node.args[1])
                                 if isinstance(n, ast.Call)
                                 and any(x is el for x in ast.walk(n))]
                    hashed = any(
                        (isinstance(c.func, ast.Attribute)
                         and c.func.attr in ("sha256", "hexdigest", "encode"))
                        for c in enclosing)
                    assert hashed, (
                        "the RAW token is passed as an INSERT value; it is a "
                        "working credential for /upgrade/h/<token> and this "
                        "telemetry table must not hold one")


def test_the_column_is_added_to_the_existing_table_not_only_a_new_one():
    """★ CREATE TABLE IF NOT EXISTS is a NO-OP on the live table. Without an
    ALTER, token_hash would exist only on a fresh database and every
    production write would fail on an unknown column."""
    src = RELAY.read_text(encoding="utf-8")
    assert re.search(r"ALTER TABLE relay_opens\"?\s*\"?\s*ADD COLUMN IF NOT EXISTS token_hash",
                     src.replace("\n", " ").replace('"', '"')), (
        "no ALTER TABLE ... ADD COLUMN IF NOT EXISTS token_hash — the column "
        "would only exist on a freshly created table")


def test_v6_identity_falls_back_to_the_token():
    src = FUNNEL.read_text(encoding="utf-8")
    assert "_v6_id" in src, "v6 has no identity expression"
    i = src.index("_v6_id =")
    expr = src[i:src.index("\n", i)]
    assert "session_id" in expr and "token_hash" in expr, (
        f"v6 identity does not coalesce session_id with token_hash: {expr!r}")
    assert "nullif" in expr.lower(), (
        "an EMPTY session_id must fall through to the token, and only NULLIF "
        "turns '' into NULL for coalesce")


def test_v6_no_longer_requires_a_nonempty_session_id():
    """The requirement it used to carry is exactly what excluded the 30."""
    src = FUNNEL.read_text(encoding="utf-8")
    i = src.index("_v6_body = (")
    body = src[i:src.index("opened_v6 = one(", i)]
    assert "coalesce(ro.session_id,'') <> ''" not in body, (
        "v6 still demands a non-empty session_id, which is the filter that "
        "reduced 32 real-UA opens to 2")


def test_v6_still_excludes_operator_self_traffic():
    """★ v3 counted the operator's own click. Widening identity must not widen
    that too."""
    src = FUNNEL.read_text(encoding="utf-8")
    i = src.index("_v6_body = (")
    body = src[i:src.index("opened_v6 = one(", i)]
    assert "_ro_not_self" in body, "v6 dropped the self-traffic exclusion"
    assert "_ro_real" in body, "v6 dropped the real-UA filter"


def test_the_basis_admits_the_fix_is_not_retroactive():
    """Rows written before this change have no token_hash and stay
    uncountable. A reader comparing to yesterday must be told."""
    src = FUNNEL.read_text(encoding="utf-8")
    i = src.index('"human_acted_v6_basis"')
    basis = src[i:i + 1400]
    assert "token_hash" in basis, "the basis does not mention the new identity"
    assert "forward" in basis or "retroactiv" in basis, (
        "the basis does not say the fix is forward-looking")


def test_the_v6_sql_carries_no_literal_percent():
    src = FUNNEL.read_text(encoding="utf-8")
    i = src.index("_v6_id =")
    seg = src[i:src.index("opened_v6 = one(", i)]
    assert "%" not in seg.replace("'%s'", ""), (
        f"a literal % in the v6 body breaks `sql % iv`: {seg!r}")


def test_the_basis_names_the_expression_the_query_actually_counts():
    """★ The basis opened with "COUNT(DISTINCT ro.session_id)" AFTER the query
    had been changed to count the coalesced identity — a published description
    contradicting its own SQL, introduced by the same commit that changed the
    SQL. A basis is only worth publishing if it tracks the thing it describes."""
    src = FUNNEL.read_text(encoding="utf-8")
    i = src.index('"human_acted_v6_basis"')
    basis = src[i:i + 1400]
    assert "coalesce" in basis.lower(), (
        "the basis does not name the coalesced identity the query counts")
    head = basis[:220]
    assert not re.search(r"COUNT\(DISTINCT ro\.session_id\)", head), (
        "the basis still opens by claiming it counts DISTINCT session_id, "
        "which is not what the query does")
