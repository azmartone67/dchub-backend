"""/AGENTS-inline.md must not type its own entity counts.

routes/quick_redirects.py::_AGENTS_MD is the inline fallback copy of AGENTS.md.
Measured 2026-09-07 it carried:

    "4,000+ tracked M&A deals"   against a measured 2,123 distinct  (~1.9x OVER)
    "126,000 substations"        against a measured 127,288         (UNDER)

★ THE DEALS FIGURE WAS A BANNED LITERAL. "4,000+" is named explicitly in
tests/test_agent_surface_floors_match_canon.RETIRED_DEAL_FLOORS — retired
2026-07-17 because it floored deal ROWS, not deals (the AUTO id embeds the
ingest date, so one deal accrues a row per day). It is the single worst number
in this repo's history of published counts, and it survived here because that
denylist is only ever scanned against the SURFACES list of discovery FILES.
No Python module is in SURFACES, so a banned literal sat in a .py untouched.

★ THIS SURFACE IS CURRENTLY UNREACHABLE, and the fix is pre-emptive on purpose.
/AGENTS-inline.md returns 404 at the edge — the path is absent from the worker's
Railway forward list, so the Flask route never runs. The live /AGENTS.md is a
DIFFERENT handler (ai_discovery_routes) and states the correct 2,100+.

That is exactly the shape the "unrouted surface rots" note describes: an
unreachable surface keeps drifting, and whoever routes it later PUBLISHES the
rot in one commit. Fixing it while it is dark is cheaper than discovering it
the day it goes live.
"""
import re

import pytest

SRC_PATH = "routes/quick_redirects.py"
SRC = open(SRC_PATH, encoding="utf-8").read()

#: Retired deal floors, restated here rather than imported: the owning module is
#: a sibling TEST, and importing test-from-test couples their collection. If the
#: two ever disagree, test_retired_lists_agree below is the tripwire.
RETIRED_DEAL_FLOORS = ("4,000+", "1,600+", "1,400+")


def _agents_md():
    pytest.importorskip("flask")
    import routes.quick_redirects as qr
    md = getattr(qr, "_AGENTS_MD", None)
    assert md, "_AGENTS_MD is gone — if the inline fallback was deleted, delete this guard with it"
    return md


def _retired_hits(text: str) -> list:
    """Retired floors present as WHOLE figures.

    ★2026-09-07 — this was `[f for f in RETIRED_DEAL_FLOORS if f in text]`, a
    bare substring test, and it produced a FALSE POSITIVE the moment a real
    number ended in one of them: "4,000+" is a substring of "94,000+", so binding
    the transmission count to canon (94,000+) made this guard report a ~1.9x
    over-claim that was not there. "1,600+" inside "21,600+" and "1,400+" inside
    "21,400+" are the same trap waiting.

    A digit or comma immediately before the match means it is the tail of a
    larger figure, not the figure itself.
    """
    return [f for f in RETIRED_DEAL_FLOORS
            if re.search(r'(?<![\d,])' + re.escape(f), text)]


def test_no_retired_deal_floor_in_the_inline_agents_copy():
    md = _agents_md()
    hits = _retired_hits(md)
    assert not hits, (
        f"/AGENTS-inline.md states retired deal floor(s) {hits}. '4,000+' floored "
        "deal ROWS, not deals — a ~1.9x over-claim on a citable surface. Render "
        "{canon_deals} instead.")


def test_the_inline_copy_states_canonical_deals_and_substations():
    md = _agents_md()
    import ai_surface_canon as canon
    nums = canon.canon_nums()
    for label, rx in (("deals", r'([\d,]+\+?)\s*tracked M&A deals'),
                      ("substations", r'([\d,]+\+?)\s*substations'),
                      # ★2026-09-07 (later): fiber + transmission joined once
                      #  {canon_fiber_routes}/{canon_transmission_lines} existed.
                      #  Before that there was NO placeholder to reach, so the copy
                      #  HAD to hardcode — and "52,000 transmission lines" was not
                      #  merely stale, it named the SUPERSEDED services1 ArcGIS
                      #  population (52.2k) rather than the published inventory.
                      ("fiber_routes", r'([\d,]+\+?)\s*fiber routes'),
                      ("transmission_lines", r'([\d,]+\+?)\s*transmission lines')):
        want = nums.get("{canon_%s}" % label)
        assert want, f"canon publishes no {label} phrase"
        got = set(re.findall(rx, md))
        assert got, (
            f"the inline copy no longer states a {label} figure at all — floor: "
            "this assertion is a scan and would pass vacuously on nothing.")
        assert got <= {want}, (
            f"/AGENTS-inline.md states {label} {sorted(got - {want})} but canon "
            f"says {want!r}")
    assert "{canon_" not in md, (
        "the inline copy serves an UNRESOLVED placeholder — worse than the stale "
        "number. _AGENTS_MD must stay wrapped in canon_text().")


def test_retired_lists_agree():
    """The tripwire for the restated constant above."""
    owner = "tests/test_agent_surface_floors_match_canon.py"
    text = open(owner, encoding="utf-8").read()
    m = re.search(r"RETIRED_DEAL_FLOORS\s*=\s*\(([^)]*)\)", text)
    assert m, f"{owner} no longer defines RETIRED_DEAL_FLOORS — re-home this check"
    theirs = tuple(re.findall(r'"([^"]+)"', m.group(1)))
    assert theirs == RETIRED_DEAL_FLOORS, (
        f"{owner} lists {theirs} but this file restates {RETIRED_DEAL_FLOORS}. "
        "Copy theirs — a denylist that disagrees with itself bans nothing "
        "reliably.")


def test_the_retired_floor_match_is_anchored():
    """The false positive that made this necessary, and the true positive it must
    still catch. Without the lookbehind the first assertion fails."""
    assert _retired_hits("- 94,000+ transmission lines") == [], (
        "'4,000+' matched inside '94,000+' — the retired-floor check is "
        "unanchored again and will report over-claims that do not exist.")
    assert _retired_hits("- 21,600+ widgets") == [], "'1,600+' matched inside '21,600+'"
    assert _retired_hits("- 4,000+ tracked M&A deals") == ["4,000+"], (
        "the anchored check stopped catching the real retired floor — it must "
        "still fire on a bare 4,000+.")
