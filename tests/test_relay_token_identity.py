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


def test_v6_keeps_the_session_requirement_that_keeps_it_deloopable():
    """★ THE CORRECTION THIS FILE EXISTS TO RECORD.

    The first version of this change widened v6 itself to
    coalesce(session_id, token_hash) and kept `_ro_not_self`, and shipped a
    test asserting the session requirement was GONE — the exact opposite of
    tests/test_human_acted_v6_anchor.py::test_v6_requires_a_session_id, which
    was in the tree and failed on it. The guard was right. `_ro_not_self` is
    external_session_predicate("ro.session_id"), and that function's own
    docstring says a NULL/empty session is KEPT ("not knowably ours"), so on
    precisely the rows the widening added it evaluates to TRUE for free. The
    widening therefore re-opened v3's bug — counting the operator's own click
    — inside the change that removed the filter which had been closing it.
    """
    src = FUNNEL.read_text(encoding="utf-8")
    i = src.index("_v6_body = (")
    body = src[i:src.index("opened_v6 = one(", i)]
    assert "coalesce(ro.session_id,'') <> ''" in body, (
        "v6 dropped its session requirement; every row it then adds is "
        "un-deloopable, because the self-traffic exclusion keys on session_id")
    counted = src[src.index("opened_v6 = one("):]
    counted = counted[:counted.index("\n")]
    assert "count(distinct ro.session_id)" in counted, (
        f"v6 counts something other than the session it filters on: {counted!r}")


def test_the_token_identity_is_published_as_its_own_number():
    """The 30 opens are not discarded — they are counted, separately, so the
    number that feeds anything stays de-loopable and the number that cannot be
    de-looped says so instead of borrowing v6's filters."""
    src = FUNNEL.read_text(encoding="utf-8")
    assert '"human_acted_v6_links_opened": opened_v6_links,' in src, (
        "the token identity is not published; the 30 rows are still invisible")
    i = src.index("_v6_links_body = (")
    body = src[i:src.index("opened_v6_links = one(", i)]
    assert "_v6_id" in body, "the links number does not use the token identity"
    assert "_ro_real" in body, (
        "the links number dropped the real-UA filter, so it counts scanners")


def test_the_links_number_does_not_borrow_an_exclusion_that_cannot_see_it():
    """★ NAME-NOT-VALUE. Asserting `_ro_not_self in body` passes whether or not
    the predicate can actually reach the rows being counted — that is how the
    widening looked guarded while being vacuous. The links number must NOT
    carry it: an exclusion that is TRUE by construction on every row it filters
    is worse than none, because the payload then reads as de-looped."""
    src = FUNNEL.read_text(encoding="utf-8")
    i = src.index("_v6_links_body = (")
    body = src[i:src.index("opened_v6_links = one(", i)]
    assert "_ro_not_self" not in body, (
        "the links body applies the self-traffic exclusion to token-only rows, "
        "where COALESCE('','') !~* '^(seed)' is TRUE for free — a filter that "
        "cannot fire, published as though it had")


def test_the_links_basis_declares_that_it_cannot_be_delooped():
    """A number no filter can clean is publishable. A number no filter can
    clean, published without saying so, is the funnel's oldest failure."""
    src = FUNNEL.read_text(encoding="utf-8")
    i = src.index('"human_acted_v6_links_opened_basis"')
    basis = src[i:src.index('"human_acted_v2_all_view_opens"', i)]
    low = basis.lower()
    assert "no operator self-traffic exclusion" in low or "no session requirement" in low, (
        "the basis does not say which filters it drops")
    assert "vacuous" in low or "un-deloopable" in low or "for free" in low, (
        "the basis does not say the exclusion CANNOT fire on these rows — "
        "listing an absent filter is not the same as explaining that applying "
        "it would have been meaningless")
    assert "upper bound" in low or "not as a count of humans" in low or \
           "never as a count of humans" in low, (
        "the basis does not tell the reader how to read the number")


def test_v6_still_excludes_operator_self_traffic():
    """★ v3 counted the operator's own click."""
    src = FUNNEL.read_text(encoding="utf-8")
    i = src.index("_v6_body = (")
    body = src[i:src.index("opened_v6 = one(", i)]
    assert "_ro_not_self" in body, "v6 dropped the self-traffic exclusion"
    assert "_ro_real" in body, "v6 dropped the real-UA filter"


def test_the_basis_admits_the_fix_is_not_retroactive():
    """Rows written before this change have no token_hash and stay
    uncountable. A reader comparing to yesterday must be told."""
    src = FUNNEL.read_text(encoding="utf-8")
    i = src.index('"human_acted_v6_links_opened_basis"')
    basis = src[i:src.index('"human_acted_v2_all_view_opens"', i)]
    assert "token_hash" in basis, "the basis does not mention the new identity"
    assert "forward" in basis or "retroactiv" in basis, (
        "the basis does not say the fix is forward-looking")


def test_the_v6_sql_carries_no_literal_percent():
    """Each BODY on its own — the slice must not swallow the `% iv` call site
    that follows it, or the test fails on the very operator it protects."""
    src = FUNNEL.read_text(encoding="utf-8")
    for start, end in (("_v6_body = (", "opened_v6 = one("),
                       ("_v6_links_body = (", "opened_v6_links = one(")):
        i = src.index(start)
        seg = src[i:src.index(end, i)].replace("'%s'", "")
        assert "%" not in seg, (
            f"a literal % in {start!r} breaks `sql % iv`: {seg!r}")


def test_each_basis_names_the_expression_its_own_query_counts():
    """★ The basis opened with "COUNT(DISTINCT ro.session_id)" AFTER the query
    had been changed to count the coalesced identity — a published description
    contradicting its own SQL, introduced by the same commit that changed the
    SQL. Now there are TWO numbers, so the failure mode is one basis describing
    the other's query. Each is checked against the SELECT it belongs to."""
    src = FUNNEL.read_text(encoding="utf-8")

    _i = src.index('"human_acted_v6_basis"')
    v6_basis = src[_i:src.index('"human_acted_v6_links_opened"', _i)]
    assert re.search(r"COUNT\(DISTINCT session_id\)", v6_basis), (
        "v6's basis does not open by naming the session count it performs")
    assert "coalesce(nullif" not in v6_basis.lower(), (
        "v6's basis claims the coalesced identity, which is the OTHER number")

    _j = src.index('"human_acted_v6_links_opened_basis"')
    lk_basis = src[_j:src.index('"human_acted_v2_all_view_opens"', _j)]
    assert "coalesce" in lk_basis.lower(), (
        "the links basis does not name the coalesced identity it counts")
