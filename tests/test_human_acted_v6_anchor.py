"""human_acted v6 counts FROM the table that records the open.

v2..v5 all read `from mcp_high_intent_sessions s` and only then ask whether a
relay row exists, so an open on a session that table never received is not a
zero — it is unreachable. The stage's own published denominator gap measured
it: 148 of 163 joinable opens sat on sessions absent from the table.

Live 2026-09-07, 30d: 174 relay opens, 142 rejected as probe UAs, 30 with no
session_id, 2 countable — and 1 of those 2 not in the table. 7d: 82 opens, 0
countable. The published 0 is an instrument limit, not an observed absence.

ast-bound against the shipped source; flask_mcp_endpoints imports flask and a
DB pool, so it is never imported here.
"""
import ast
import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[1] / "flask_mcp_endpoints.py"


def _src() -> str:
    return SRC.read_text(encoding="utf-8")


def _v6_assign() -> str:
    """The literal source of the _v6_body assignment."""
    src = _src()
    i = src.index("_v6_body = (")
    j = src.index("opened_v6 = one(", i)
    return src[i:j]


def test_v6_is_anchored_on_relay_opens_not_the_sessions_table():
    body = _v6_assign()
    assert "from relay_opens ro" in body, (
        "v6 must read FROM relay_opens — anchoring on mcp_high_intent_sessions "
        "is the exact blind spot it exists to remove")
    assert "mcp_high_intent_sessions" not in body, (
        "v6 still joins the sessions table, so it inherits the same gap")


def test_v6_keeps_the_real_ua_filter():
    body = _v6_assign()
    assert "_ro_real" in body, (
        "v6 dropped the real-UA predicate; re-anchoring without it counts "
        "probe traffic as humans")


def test_v6_keeps_the_operator_self_traffic_exclusion():
    """★ The reason v4 and v5 exist. v3 counted the operator's own click and
    published the stage's first non-zero. Re-anchoring without this exclusion
    reintroduces precisely that bug."""
    body = _v6_assign()
    assert "_ro_not_self" in body, (
        "v6 has no self-traffic exclusion — v3's bug, one definition later")
    src = _src()
    assert '_ro_not_self = _deloop_external_session_predicate("ro.session_id")' in src, (
        "the exclusion is not derived from the declared deloop seed")


def test_v6_requires_a_session_id():
    body = _v6_assign()
    assert "coalesce(ro.session_id,'') <> ''" in body, (
        "v6 would count rows with no session id, which cannot be deduplicated "
        "per session or excluded as self-traffic")


def test_v6_carries_no_literal_percent():
    """★ THIS FILE HAS TAKEN THE ENDPOINT DOWN ON EXACTLY THIS. A predicate
    rendered with LIKE '88e20dac%' broke `sql % iv` inside one deploy:
    {"error": "not enough arguments for format string"}. Only the window
    interval's own %s may appear."""
    body = _v6_assign()
    assert body.count("%") == 1 and "'%s'" in body, (
        f"v6 body carries a literal % besides the interval placeholder: {body!r}")


def test_human_acted_is_still_v5_not_silently_promoted():
    """A stage that has been wrong-non-zero twice does not get a new definition
    installed on the headline before its number is read against live data."""
    src = _src()
    steps = src[src.index('steps = {"paywall_hit"'):src.index("return {", src.index('steps = {"paywall_hit"'))]
    assert '"human_acted": opened,' in steps, (
        "the headline stage was repointed at v6 in the same change that "
        "introduced it")
    assert "opened_v6" not in steps, "v6 leaked into the published steps dict"


def test_v6_is_published_with_a_basis():
    src = _src()
    assert '"human_acted_v6_from_relay_opens": opened_v6,' in src
    assert '"human_acted_v6_basis"' in src, (
        "v6 ships without a basis; a second number for the same stage with no "
        "stated difference is how two counts drift apart unnoticed")
    basis = re.search(r'"human_acted_v6_basis": \(\s*(.*?)\),\n', src, re.S).group(1)
    assert "session_id" in basis and "relay_opens" in basis, (
        "the basis does not name the remaining blind spot (opens with no "
        "session_id) or the table it reads")
