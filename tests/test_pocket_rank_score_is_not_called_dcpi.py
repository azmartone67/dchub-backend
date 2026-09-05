"""/pockets ranks on its own formula; it may not publish it as the DCPI composite.

r-pocket-score-label (2026-09-05).

WHAT WAS MEASURED
-----------------
Live through the edge, cache-busted 2026-09-05, after the canon split was
fixed (r-market-canon-split):

    /pockets/ashburn            "DCPI composite score -4.3"
    /pockets/northern-virginia  "DCPI composite score -109.6"
    /dcpi/ashburn               "DCPI 27.4"

Two of those three numbers were the same market. The -109.6 was the retired
twin and r-market-canon-split removed it. The -4.3 is NOT a stale row and NOT
a wrong number — it is `rank_score`, this page's real deployability ordering:

    excess − constraint/2 − max(0, ttp−24)×2  ± verdict bonus

The DCPI composite is a different function, routes/dcpi.derive_composite_score:
60/30/10 weights across the same three components, times a verdict quality
multiplier, and — the part the label broke — BOUNDED 0-100.
util/market_entity publishes it as "buildability composite scored 0-100". So
"DCPI composite score -4.3" was not a rounding disagreement; it was a value
outside the declared range of the quantity the label named, in the meta
description and the ld+json `description` that agents read without loading the
page.

WHAT THIS GUARD ASSERTS
-----------------------
The templates are RENDERED and the output inspected, because every one of
these strings is a Jinja template: a constant can be correct while the
template still hard-codes the old words next to it, and `assert "DCPI
composite" not in source` cannot tell an emitted string from a comment.

  A. No rendered /pockets surface calls `rank_score` a DCPI or composite score.
  B. Every surface that prints the number also prints its name, so the number
     never travels anonymously.
  C. The ld+json parses, and its description states what the number is NOT.
  D. The label itself does not contain "DCPI", and the basis names the range.
  E. main.py's MCP recommendation payload binds `rank_score` to a
     pocket-scoped key — it sat one key away from a /dcpi/<slug> URL under the
     name `score`.

Never imports main (the green-main house rule); (E) is read from main.py's AST.
"""
import ast
import json
import os
import re

import flask
import pytest
from flask import render_template_string

from routes import pockets as pk

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


#: Every way the page could claim to be publishing the OTHER number.
#:
#: ★ THESE ARE NOT BANNED OUTRIGHT — the fixed page says "not the 0-100 DCPI
#: composite", which contains the phrase and is the correct copy. A blunt
#: substring ban failed on the very disclaimer it should have been asking for.
#: The property is narrower and it is the real one: the page may name the DCPI
#: composite ONLY to disown this number. Every occurrence must be NEGATED.
#:
#: "DCPI verdict" and "DCPI scores" are not listed at all — the verdict and the
#: three component scores really are DCPI columns straight off
#: market_power_scores. Only the composite is not.
CLAIMS = ("dcpi composite", "composite score")

#: A negation this close in front of the phrase turns a claim into a
#: disclaimer. Deliberately tight: "not" three words back still governs the
#: phrase, a "not" two sentences back does not.
_NEGATION_WINDOW = 40
_NEGATION = re.compile(r"\b(not|never|isn't|is not|rather than|unlike)\b")

#: Distinctive and NEGATIVE — negative is the whole point, since the DCPI
#: composite cannot be. A guard that probed with a positive value would pass
#: on a page that had merely relabelled the plausible cases.
PROBE_SCORE = -4.3

_DETAIL = {
    "market_slug": "ashburn", "market_name": "Ashburn", "iso": "PJM",
    "state": "VA", "verdict": "AVOID", "excess_power_score": 46.1,
    "constraint_score": 60.0, "time_to_power_months": 36,
    "rank_score": PROBE_SCORE, "computed_at": "2026-09-05T18:22:54+00:00",
    "delta_7d": None, "why": "high grid constraint (60)",
    "history_30d": [], "comparables": [],
}

_ROW = dict(_DETAIL, personal_score=None, locked=False)


def _render(template, **ctx):
    app = flask.Flask(__name__)
    with app.app_context():
        return render_template_string(template, **pk._rank_context(), **ctx)


def _detail_html():
    return _render(pk._POCKET_DETAIL_HTML, d=_DETAIL)


def _index_html(paid=True):
    return _render(
        pk._POCKETS_PAGE_HTML, as_of="2026-09-05T00:00:00Z", pockets=[_ROW],
        shown=1, caller_tier="pro" if paid else "anonymous",
        truncated_by_tier=0, upgrade_url="https://dchub.cloud/pricing",
        top_mover=None, paid=paid, build_count=0, avoid_count=1,
        total_known=1)


def _rss_xml():
    return _render(pk._POCKETS_RSS, pockets=[_ROW],
                   build_date="Fri, 05 Sep 2026 00:00:00 GMT")


SURFACES = {
    "/pockets/<slug>":   _detail_html,
    "/pockets (paid)":   lambda: _index_html(paid=True),
    "/pockets (anon)":   lambda: _index_html(paid=False),
    "/pockets.rss":      _rss_xml,
}


# ── anti-vacuity ────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", sorted(SURFACES))
def test_the_surface_renders_something_to_check(name):
    out = SURFACES[name]()
    assert len(out) > 500, f"{name} rendered {len(out)} chars — nothing to check"


@pytest.mark.parametrize("name", ["/pockets/<slug>", "/pockets (paid)",
                                  "/pockets.rss"])
def test_the_surface_actually_prints_the_number(name):
    """A template that stopped printing rank_score would satisfy every
    'does not say DCPI composite' assertion below without being fixed."""
    assert str(PROBE_SCORE) in SURFACES[name](), (
        f"{name} does not print rank_score at all — the label assertions "
        f"would pass vacuously")


# ── A. the number is not published as the DCPI composite ────────────────
_TAG = re.compile(r"<[^>]+>")
_ATTR = re.compile(r'(?:content|title)="([^"]*)"')


def _prose(out):
    """What a reader or an agent actually reads, with markup removed.

    Proximity has to be measured on PROSE. The page says
    `not the <a href="/dcpi/ashburn" style="...">DCPI composite</a>` — a
    correct disclaimer whose "not" is 66 characters of href and style away
    from the phrase. Counting markup toward the negation window flagged the
    fix as the defect.

    Attribute values are appended rather than dropped, because the meta
    description and og:description are exactly where the original claim lived.
    ld+json needs no special handling: it is text between tags, so it survives
    the strip.
    """
    body = _TAG.sub(" ", out)
    attrs = " || ".join(_ATTR.findall(out))
    return re.sub(r"\s+", " ", body + " || " + attrs).lower()


def _unnegated(out, phrase):
    """Offsets where `phrase` appears as a CLAIM rather than a disclaimer."""
    hits, i = [], out.find(phrase)
    while i != -1:
        if not _NEGATION.search(out[max(0, i - _NEGATION_WINDOW):i]):
            hits.append(i)
        i = out.find(phrase, i + 1)
    return hits


@pytest.mark.parametrize("name", sorted(SURFACES))
@pytest.mark.parametrize("phrase", CLAIMS)
def test_no_surface_calls_the_rank_score_a_dcpi_composite(name, phrase):
    out = _prose(SURFACES[name]())
    hits = _unnegated(out, phrase)
    assert not hits, (
        f"{name} publishes rank_score as {phrase!r}, unnegated, at "
        f"{hits}: ...{out[max(0, hits[0] - 60):hits[0] + 60]!r}... "
        f"The DCPI composite is routes/dcpi.derive_composite_score, bounded "
        f"0-100 and served at /dcpi/<market>; rank_score is unbounded and was "
        f"published live at {PROBE_SCORE} for a market whose DCPI composite "
        f"was 27.4.")


def test_the_negation_window_can_still_see_a_claim():
    """The rule above only works if a bare claim is still caught. Without
    this, widening _NEGATION, the window, or _prose silently disarms every
    case — and _prose is the easiest of the three to widen by accident."""
    assert _unnegated("dcpi composite score -4.3", "dcpi composite") == [0]
    assert _unnegated("not the 0-100 dcpi composite", "dcpi composite") == []
    # through the real markup path, both ways round
    claim = '<meta name="description" content="DCPI composite score -4.3">'
    ok = '<div class="x">not the <a href="/dcpi/ashburn" style="color:inherit">DCPI composite</a></div>'
    assert _unnegated(_prose(claim), "dcpi composite"), (
        "_prose swallowed a real claim — every surface test is now vacuous")
    assert not _unnegated(_prose(ok), "dcpi composite")


# ── B. the number never travels anonymously ─────────────────────────────
@pytest.mark.parametrize("name", ["/pockets/<slug>", "/pockets (paid)",
                                  "/pockets.rss"])
def test_every_surface_that_prints_the_number_names_it(name):
    out = SURFACES[name]()
    assert pk.POCKET_RANK_LABEL.lower() in out.lower(), (
        f"{name} prints {PROBE_SCORE} without {pk.POCKET_RANK_LABEL!r} "
        f"anywhere — an unnamed score next to DCPI verdicts reads as a DCPI "
        f"score, which is how this defect started")


def test_the_hero_stat_itself_carries_the_label():
    """Anywhere-on-the-page is not enough, and this is not hypothetical: a
    mutation reverting the hero label to a bare "Score" survived the test
    above, because the footer still said "Pocket rank score" 3000 characters
    later. The hero is the number a reader and a screenshot actually see, so
    the label has to be IN it."""
    html = _detail_html()
    blocks = re.findall(r'<div class="hero-stat">.*?</div></div>', html, re.S)
    assert blocks, "no hero-stat blocks on the pocket detail page"
    owning = [b for b in blocks if str(PROBE_SCORE) in b]
    assert len(owning) == 1, (
        f"expected exactly one hero stat showing {PROBE_SCORE}, got "
        f"{len(owning)}")
    assert pk.POCKET_RANK_LABEL in owning[0], (
        f"the hero stat displays {PROBE_SCORE} without naming it:\n"
        f"{owning[0]}")


# ── C. the machine-readable half carries the disclaimer ─────────────────
def test_the_json_ld_parses_and_says_what_the_number_is_not():
    html = _detail_html()
    m = re.search(r'<script type="application/ld\+json">\s*(\{.*?\})\s*</script>',
                  html, re.S)
    assert m, "no ld+json block on the pocket detail page"
    node = json.loads(m.group(1))            # raises if autoescape broke it
    desc = node["description"]
    assert pk.POCKET_RANK_LABEL in desc
    assert "NOT the DCPI composite" in desc, (
        "the ld+json description is what an agent reads without loading the "
        f"page; it must disown the name it used to carry. Got: {desc!r}")
    assert "&#" not in m.group(1), (
        "an HTML entity reached the ld+json block — a JSON parser does not "
        "decode entities, so the agent reads it literally. Keep ' & < out of "
        "POCKET_RANK_* (that is why POCKET_RANK_BASIS has no apostrophe).")


def test_the_meta_description_keeps_the_disclaimer_inside_the_truncation_window():
    """Search engines cut around 160 characters. The whole point of the short
    form is that the 'not the DCPI composite' half survives the cut."""
    html = _detail_html()
    m = re.search(r'<meta name="description" content="([^"]*)"', html)
    assert m, "no meta description on the pocket detail page"
    desc = m.group(1)
    assert pk.POCKET_RANK_SHORT in desc, desc
    assert len(desc) <= 200, (
        f"meta description is {len(desc)} chars; the disclaimer is at "
        f"character {desc.find(pk.POCKET_RANK_SHORT)} and would be truncated "
        f"away, leaving a bare number beside a DCPI verdict")


# ── D. the constants themselves ─────────────────────────────────────────
def test_the_label_does_not_claim_dcpi():
    assert "dcpi" not in pk.POCKET_RANK_LABEL.lower(), pk.POCKET_RANK_LABEL
    assert "composite" not in pk.POCKET_RANK_LABEL.lower(), pk.POCKET_RANK_LABEL


def test_the_basis_names_the_range_that_the_old_label_violated():
    basis = pk.POCKET_RANK_BASIS
    assert "0-100" in basis, "the basis must name the DCPI composite's range"
    assert "negative" in basis.lower(), (
        "the basis must say this number can be negative — that is the fact "
        "that made 'DCPI composite score -4.3' impossible on its face")
    assert "NOT the DCPI composite" in basis


def test_the_two_numbers_really_are_different_functions():
    """If the pockets formula were ever changed to the DCPI composite, the
    label would stop being a lie and every assertion above would be wrong —
    so pin that they are still two formulas.

    Executed against the real scorer, not asserted from a comment.
    """
    from routes.dcpi import derive_composite_score
    excess, constraint, ttp, verdict = 46.1, 60.0, 36, "AVOID"
    ttp_penalty = max(0, ttp - 24) * 2
    rank = excess - (constraint * 0.5) - ttp_penalty - 20   # AVOID bonus
    composite = derive_composite_score(excess, constraint, ttp, verdict)
    assert rank != composite
    assert rank < 0 <= composite <= 100, (
        f"rank={rank} composite={composite} — the guard's premise is that one "
        f"is unbounded and the other is 0-100")


# ── E. the MCP recommendation payload ───────────────────────────────────
def _live_pocket_keys():
    """The key each value in main.py's `live_pocket` dict literal is bound to.

    AST, not substring: tests never import main (routes/dcpi builds MARKETS at
    import time, which needs a DB). Reading the dict STRUCTURE rather than
    grepping for a word is what makes this fail on a rename back — the word
    "score" appears dozens of times in that file.
    """
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "live_pocket"
                and isinstance(node.value, ast.Dict)):
            continue
        out = {}
        for k, v in zip(node.value.keys, node.value.values):
            if isinstance(k, ast.Constant):
                out[k.value] = ast.unparse(v)
        if out:
            return out
    raise AssertionError("no `live_pocket = {...}` dict literal in main.py — "
                         "this guard is reading nothing")


def test_the_recommendation_payload_names_the_score_it_publishes():
    keys = _live_pocket_keys()
    bound = [k for k, v in keys.items() if "rank_score" in v]
    assert bound, f"live_pocket no longer carries rank_score: {sorted(keys)}"
    for k in bound:
        assert "pocket_rank" in k, (
            f"live_pocket publishes rank_score under {k!r}. It sits beside a "
            f"dchub.cloud/dcpi/<slug> URL, so a bare 'score' reads as that "
            f"page's DCPI composite — a different, 0-100 number.")
    assert "pocket_rank_basis" in keys, (
        "the payload states the number but not what it is; an agent quoting "
        "it has nothing to disclose")
