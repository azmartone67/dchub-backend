"""The FULL docs must state every floor the SUMMARY docs state.

★ MEASURED LIVE 2026-09-03, with the audit-closure checker's own regexes:

    llms.txt        facilities=['20,100+']  deals=['2,000+']
    llms-full.txt   facilities=['20,100+']  deals=[]          ← nothing at all

audit-closure lane E has carried SH52-028 as FAIL on exactly this.

★ THE FLOOR WAS NOT STALE, IT WAS ABSENT. SH52-028 is filed as "llms-full.txt
is three canon generations stale (15,000+ facilities / 1,500+ deals)", and the
facilities half was healed — /llms-full.txt already carries {canon_facilities}.
The deals half was never a drifting number: the inline block that serves
/llms-full.txt documented GET /api/v1/transactions without ever saying how many
deals are tracked. A heal pass looking for a stale figure to bump finds nothing
to bump, reports clean, and the gap survives every sweep.

That is why this guard compares the two surfaces to EACH OTHER instead of
checking llms-full against a literal: a missing number and a correct number
look identical to any check that only asks "is the number here wrong?".

★ The static llms-full.txt / static/llms-full.txt files in the repo are NOT
what serves. /llms-full.txt is an inline canon_text() block in
ai_discovery_routes.register_discovery_routes; ai_agent_discovery.serve_llms_full
reads the static file and is shadowed by it. Those files still read 18,500+ /
1,900+, so a guard pointed at them would pass while the live surface stayed
short. This drives the real route through a Flask test client.
"""
from __future__ import annotations

import re

import pytest


def _floors(text: str, noun_re: str) -> list[str]:
    """The audit-closure checker's own regex (routes/audit_closure_master_shell
    ._lane_surfaces._floors), reproduced so this guard and the lane agree on
    what a 'floor' is."""
    return sorted(set(re.findall(
        r"([\d][\d,]*\+)\s+(?:[a-z-]+\s+){0,3}" + noun_re, text, re.I)))


_FACILITIES = r"facilit"
_DEALS = r"(?:M&A\s+)?(?:transactions|deals)"


@pytest.fixture(scope="module")
def served():
    flask = pytest.importorskip("flask")
    from ai_discovery_routes import register_discovery_routes
    app = flask.Flask(__name__)
    register_discovery_routes(app)
    c = app.test_client()
    out = {}
    for path in ("/llms.txt", "/llms-full.txt"):
        r = c.get(path)
        assert r.status_code == 200, "%s -> %s" % (path, r.status_code)
        out[path] = r.get_data(as_text=True)
    return out


def test_both_surfaces_state_a_facility_floor(served):
    for path, body in served.items():
        assert _floors(body, _FACILITIES), path


def test_both_surfaces_state_a_deal_floor(served):
    """The regression. Before this change /llms-full.txt returned []."""
    for path, body in served.items():
        assert _floors(body, _DEALS), (
            "%s states no deal floor — the full documentation is silent on a "
            "number the summary publishes" % path)


def test_the_two_surfaces_agree(served):
    """SH52-028's actual invariant: full docs must not contradict summary docs."""
    a, b = served["/llms.txt"], served["/llms-full.txt"]
    assert _floors(a, _FACILITIES) == _floors(b, _FACILITIES)
    assert _floors(a, _DEALS) == _floors(b, _DEALS)


def test_each_surface_states_exactly_ONE_floor_per_noun(served):
    """A file that contradicts itself is the SH52-027/125 class. Two different
    facility floors in one file is worse than one stale floor."""
    for path, body in served.items():
        for noun, label in ((_FACILITIES, "facilities"), (_DEALS, "deals")):
            got = _floors(body, noun)
            assert len(got) == 1, "%s carries %d %s floors: %s" % (
                path, len(got), label, got)


def test_the_floors_are_DERIVED_not_typed(served):
    """★ NARROWNESS. Typing '2,000+' into the block would satisfy every test
    above and drift the day canon moves. The floor must come from the
    {canon_deals} placeholder, so it can only ever be the canonical value."""
    import inspect

    import ai_discovery_routes as adr
    src = inspect.getsource(adr.register_discovery_routes)
    full = src[src.index("@app.route('/llms-full.txt')"):]
    end = full.find("@app.route(", 10)
    full = full[:end] if end > 0 else full
    assert "{canon_deals}" in full, (
        "the deal floor must be the {canon_deals} placeholder, not a literal")
    assert not re.search(r"[\d][\d,]*\+\s+(?:[a-z-]+\s+){0,3}"
                         r"(?:M&A\s+)?(?:transactions|deals)", full, re.I), (
        "a hardcoded deal floor was typed into the llms-full block")


def test_an_unrendered_placeholder_never_reaches_a_reader(served):
    """canon_text substitutes by literal placeholder; a typo'd name would ship
    the braces verbatim to every agent that reads the file."""
    for path, body in served.items():
        assert "{canon_" not in body, path
