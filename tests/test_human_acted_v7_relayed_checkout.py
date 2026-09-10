"""r-third-artifact (2026-09-10) — the funnel must be able to see the link
agents actually relay.

WHAT THIS GUARDS. Measured from outside, anonymously, on 2026-09-09: a single
`tools/call` to a gated tool on https://dchub.cloud/mcp returns an envelope
whose two halves carry DIFFERENT links.

    content[0].text     "👤 Tell your human: … → https://dchub.cloud/go/c/<token>"
    structuredContent   .for_your_human.url = https://dchub.cloud/upgrade/h/<t>

`human_acted` reads relay_opens, and only /upgrade/h/ writes it. /go/c/<token>
is a different blueprint writing a different table, and the handoff funnel had
no read of that table at all — so a human who clicks the link their agent put
in front of them could not move the stage that exists to measure exactly that.
Not "did not". Could not, for any click, forever.

THE v6 LESSON IS THE HARD PART, and most of these checks are about it rather
than about the new number. mcp_calls_deloop.external_session_predicate KEEPS
anything that is not knowably ours, so on a ref that is a key hash ('pk-'/'k-')
or an anonymous offer id ('a-') the operator self-traffic exclusion passes
VACUOUSLY: it looks applied and tests nothing. v6 shipped that widening once,
its own guard caught it, and the answer was two numbers — one de-loopable, one
declared as an un-de-loopable ceiling. The same split is required here and
test_v7_deloops_only_where_the_exclusion_can_bind is what holds it.

Every check binds to a VALUE the code produces (a predicate built by calling
the canonical builder, a table name read out of the writer's own INSERT) rather
than to a retyped copy of it. A guard that restates what it checks is the
defect class routes/handoff_definition.py exists to stop.
"""
from __future__ import annotations

import ast
import os
import re

import pytest

from mcp_calls_deloop import (
    external_session_predicate,
    real_ua_predicate,
)
from routes import handoff_definition as H

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TRACKER = os.path.join(_ROOT, "routes", "checkout_click_tracker.py")
_ENDPOINT = os.path.join(_ROOT, "flask_mcp_endpoints.py")

_IV = "30 days"

_V7_PAYLOAD_KEYS = (
    "human_acted_v7_from_checkout_clicks",
    "human_acted_v7_links_clicked",
    "relayed_checkout_provenance",
)


def _sqls() -> dict:
    return {
        "count": H.human_acted_v7_count_sql(_IV),
        "links": H.human_acted_v7_links_sql(_IV),
        "provenance": H.relayed_checkout_provenance_sql(_IV),
    }


def _written_table() -> str:
    """The table /go/c/<token> actually INSERTs into, read from its writer.

    Derived, never retyped: if checkout_click_tracker renames its table this
    test fails instead of quietly guarding a table nobody writes.
    """
    tree = ast.parse(open(_TRACKER, encoding="utf-8").read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for m in re.finditer(r"INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)",
                                 node.value, re.I):
                names.add(m.group(1))
    assert len(names) == 1, (
        "expected exactly one INSERT target in the checkout-click writer, "
        "found %r — this test can no longer say which table the relayed link "
        "writes" % (sorted(names),))
    return names.pop()


def test_v7_reads_the_table_the_relayed_link_writes():
    table = _written_table()
    assert table and table != "relay_opens"
    for name, sql in _sqls().items():
        assert (" " + table + " ") in sql, (
            "%s does not read %s — the table /go/c writes" % (name, table))
        assert "relay_opens" not in sql, (
            "%s reads relay_opens; that is the OTHER artifact, already "
            "counted by human_acted_v6" % name)


def test_the_relayed_link_endpoint_is_still_go_c():
    """The module comments claim the relayed link is /go/c/<token>. Pin it.

    If the route moves, every basis string published beside these numbers
    starts describing a URL that no longer exists.
    """
    src = open(_TRACKER, encoding="utf-8").read()
    routes = re.findall(r"@\w+\.route\(\s*[\"']([^\"']+)[\"']", src)
    assert "/go/c/<token>" in routes, routes


def test_v7_requires_a_signed_link():
    """sig_ok is the only thing separating a relayed link from a scanner."""
    signed = H.relayed_checkout_signed()
    assert "sig_ok" in signed and len(signed) > 5, signed
    for name in ("count", "links"):
        assert signed in _sqls()[name], (
            "%s counts clicks it cannot prove came from a link we minted" % name)


def test_v7_uses_the_canonical_real_ua_predicate():
    """The SAME predicate the stage applies, on this table's column.

    Compared against a fresh call to mcp_calls_deloop, so a change to the
    probe-UA families moves both together and a retyped copy fails here.
    """
    want = real_ua_predicate("cc.user_agent")
    assert H.relayed_checkout_real_ua() == want
    for name, sql in _sqls().items():
        assert want in sql, "%s does not filter probe UAs" % name


def test_v7_deloops_only_where_the_exclusion_can_bind():
    """★ THE v6 LESSON. Two numbers, not one wider number.

    The de-loopable count restricts to the one ref_kind the operator
    self-traffic exclusion can actually test. The ceiling applies NEITHER, and
    must not pretend to: an exclusion that passes vacuously is worse than an
    absent one, because the basis then claims a filter that is not filtering.
    """
    sqls = _sqls()
    deloop = external_session_predicate("cc.ref")
    kind_clause = "cc.ref_kind = '%s'" % H.RELAYED_CHECKOUT_DELOOPABLE_REF_KIND

    assert deloop in sqls["count"], "the de-loopable count does not de-loop"
    assert kind_clause in sqls["count"], (
        "the de-loopable count is not restricted to the ref_kind the "
        "exclusion can bind to — on a 'pk-'/'k-'/'a-' ref it passes vacuously")

    assert deloop not in sqls["links"], (
        "the ceiling applies the self-traffic exclusion to refs it cannot "
        "test; that is the v6 widening bug arriving through a new door")
    assert kind_clause not in sqls["links"], (
        "the ceiling is restricted to one ref_kind, so it is not a ceiling")


def test_the_deloopable_ref_kind_is_one_the_writer_actually_mints():
    """'session' must stay a value routes/checkout_click_tracker._ref_kind
    returns. A filter on a ref_kind nothing writes silently counts zero."""
    src = open(_TRACKER, encoding="utf-8").read()
    tree = ast.parse(src)
    minted = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_ref_kind":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and sub.value is not None:
                    for c in ast.walk(sub.value):
                        if isinstance(c, ast.Constant) and isinstance(c.value, str):
                            minted.add(c.value)
    assert minted, "could not read _ref_kind's return values"
    assert H.RELAYED_CHECKOUT_DELOOPABLE_REF_KIND in minted, (
        "%r is not a ref_kind the writer mints; it mints %r"
        % (H.RELAYED_CHECKOUT_DELOOPABLE_REF_KIND, sorted(minted)))


def test_provenance_branches_partition_exhaustively():
    """Every row lands in exactly one branch, for all four (UA, signed) states.

    EVALUATED, not read. The two atoms are substituted with Python booleans and
    the branch conditions — the real strings the SQL is built from — are then
    evaluated as written: `and`, `not` and parentheses mean the same thing in
    both languages, so nothing is re-implemented here. A dropped `not`, a
    swapped operator or an overlapping pair fails.
    """
    branches = H.relayed_checkout_provenance_branches()
    assert len(branches) == 3, branches

    ua_atom = H.relayed_checkout_real_ua()
    sig_atom = H.relayed_checkout_signed()

    for real_ua in (True, False):
        for signed in (True, False):
            hits = []
            for name, cond in branches:
                expr = cond.replace(ua_atom, "U").replace(sig_atom, "S")
                assert re.fullmatch(r"[USandnot()\s]+", expr), (
                    "branch %r did not reduce to the two atoms: %r"
                    % (name, expr))
                if eval(expr, {"__builtins__": {}},  # noqa: S307
                        {"U": real_ua, "S": signed}):
                    hits.append(name)
            assert len(hits) == 1, (
                "real_ua=%s signed=%s matched %r — the split must be "
                "exhaustive and mutually exclusive or the published total "
                "does not add up" % (real_ua, signed, hits))


def test_provenance_names_every_subset_as_a_subset():
    """A subset is never a branch. Adding one to the other double-counts.

    Floor: there must BE subsets. A version of this test that only iterated
    whatever the module declared would pass on an empty tuple.
    """
    subsets = H.relayed_checkout_provenance_subsets()
    assert len(subsets) >= 2, subsets
    sql = H.relayed_checkout_provenance_sql(_IV)
    branch_names = [n for n, _ in H.relayed_checkout_provenance_branches()]
    for name, _cond in subsets:
        assert (" as " + name) in sql, "%s is declared but not selected" % name
        assert name not in branch_names, (
            "%s is published as a branch of an exhaustive partition; it is a "
            "subset and adding it to the branches double-counts" % name)
    basis = H.RELAYED_CHECKOUT_PROVENANCE_BASIS
    assert "SUBSET" in basis and "ORTHOGONAL" in basis, basis
    for name, _cond in subsets:
        assert name in basis, "%s is published with no basis entry" % name


def test_the_ceiling_and_the_no_ref_subset_reconcile():
    """★ minted_link_clicks - minted_link_clicks_no_ref == what the ceiling sees.

    The first live read of this block, 2026-09-10 over 30d, published
    minted_link_clicks 1 beside human_acted_v7_links_clicked 0. That pair is
    correct — a signed click with an empty ref has no identity to count
    distinctly, and unlike relay_opens this table stores no token-hash
    fallback — but with nothing naming the gap it reads as an arithmetic error
    in the split. This binds the two conditions as exact complements so the
    reconciliation stays true rather than staying merely asserted in prose.
    """
    has_ref = "coalesce(cc.ref,'') <> ''"
    no_ref = "coalesce(cc.ref,'') = ''"

    ceiling = H.human_acted_v7_links_sql(_IV)
    assert has_ref in ceiling, (
        "the ceiling does not require a ref, so it cannot be reconciled "
        "against minted_link_clicks_no_ref")

    subsets = dict(H.relayed_checkout_provenance_subsets())
    assert "minted_link_clicks_no_ref" in subsets, sorted(subsets)
    cond = subsets["minted_link_clicks_no_ref"]
    assert no_ref in cond, cond
    assert has_ref not in cond, (
        "minted_link_clicks_no_ref counts rows that DO carry a ref — it is "
        "then not the complement of what the ceiling sees")
    # and it must be scoped to the minted branch, or it is not a subset of it.
    assert H.relayed_checkout_signed() in cond, cond
    assert H.relayed_checkout_real_ua() in cond, cond


@pytest.mark.parametrize("iv", ["30 days", "7 days", "24 hours"])
def test_assembled_sql_carries_no_percent_and_no_ilike(iv):
    """A literal % took the live handoff funnel down inside one deploy.

    The caller interpolates its window with `sql % iv`, so a stray % anywhere
    in an assembled statement raises "not enough arguments for format string"
    at request time. ILIKE is banned for the same reason (it invites one).
    """
    built = {
        "count": H.human_acted_v7_count_sql(iv),
        "links": H.human_acted_v7_links_sql(iv),
        "provenance": H.relayed_checkout_provenance_sql(iv),
    }
    for name, sql in built.items():
        assert len(sql) > 200, "%s looks empty (%d chars)" % (name, len(sql))
        assert iv in sql, "%s dropped its window" % name
        assert "%" not in sql, "%s carries a literal %%" % name
        assert "ilike" not in sql.lower(), "%s uses ILIKE" % name
        # And it survives the interpolation the caller performs.
        assert (sql % ()) == sql


def test_endpoint_publishes_the_values_it_derives():
    """The payload keys must bind to computed names, never to literals.

    A published number typed in as a constant is the defect
    routes/handoff_definition.py was written to stop, and a guard that only
    greps for the key name stays green after the derivation is replaced by a 0.
    """
    tree = ast.parse(open(_ENDPOINT, encoding="utf-8").read())
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and k.value in _V7_PAYLOAD_KEYS:
                found[k.value] = v
    assert set(found) == set(_V7_PAYLOAD_KEYS), (
        "expected %r in the payload, found %r"
        % (sorted(_V7_PAYLOAD_KEYS), sorted(found)))
    for key, value in found.items():
        assert isinstance(value, ast.Name), (
            "%s is published as %s, not as a name bound to a query result"
            % (key, type(value).__name__))


def test_endpoint_derives_the_sql_from_the_one_writer():
    """The endpoint must CALL the builders, not carry its own copy of the SQL.

    Two writers of one definition is exactly how the four prior human_acted
    restatements rotted.
    """
    tree = ast.parse(open(_ENDPOINT, encoding="utf-8").read())
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    imported = {(a.asname or a.name) for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom) for a in n.names}
    for fn in ("human_acted_v7_count_sql", "human_acted_v7_links_sql",
               "relayed_checkout_provenance_sql"):
        alias = next((a.asname for n in ast.walk(tree)
                      if isinstance(n, ast.ImportFrom)
                      for a in n.names if a.name == fn), None)
        assert alias or fn in imported, (
            "%s is not imported by the endpoint" % fn)
        assert (alias or fn) in called, (
            "%s is imported but never called" % fn)
    # ★ AST, NOT THE SOURCE TEXT. The comment that explains this change names
    # the table on purpose; only a STRING carrying it would be a second writer.
    table = _written_table()
    spelled = [n for n in ast.walk(tree)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and table in n.value]
    assert not spelled, (
        "the endpoint carries %s in a string literal — that is a second "
        "writer of the definition; it must come from "
        "routes/handoff_definition (lines %r)"
        % (table, [n.lineno for n in spelled]))
