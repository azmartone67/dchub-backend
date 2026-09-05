"""DCPI market counts must be DERIVED on the surfaces #3816 did not reach.

#3816 bound the retired count on six agent-facing surfaces and added
"285 market" / "285 US" to ai_surface_canon's stale_markers. Five more
surfaces kept publishing a hand-typed DCPI market count afterwards, and the
sentinel's substring scan does not see them: two carried a DIFFERENT retired
number, and one writes the count on the far side of its label.

  * routes/partner_landing.py hardcoded a bullet while _CANON_MKTS sat defined
    at the top of that same file and the two bullets on either side of it
    resolved theirs.
  * routes/case_studies_landing.py served one as an HTML stat tile, one <div>
    from the derived facilities tile. RETIRED 2026-09-04 (#3831): the page
    it fenced was never reachable at the edge, so its entry and its render
    test are gone and four publishers remain.
  * routes/mcp_outreach_drafts.py listed one under a heading reading
    "Stats (live)", between two genuinely live siblings.
  * routes/dcpi_auto_press.py put one in the body of EVERY auto-generated
    press release, which ships to LinkedIn / X / Bluesky unreviewed.
  * routes/narrative_arc.py put one in the LAST-RESORT fallback arc — the copy
    that publishes precisely when every other source has failed.

This module fences the class, not the value. No live figure is written here on
purpose: an expected count in a test rots exactly like the literal it replaced,
so the assertions compare surfaces against the canon at run time instead.
"""
import pathlib
import re

import pytest

import ai_surface_canon
import canonical_stats

_ROOT = pathlib.Path(__file__).resolve().parents[1]

# The surfaces this change bound. A new surface that needs the number
# belongs here too.
_PUBLISHERS = [
    "routes/mcp_outreach_drafts.py",
    "routes/dcpi_auto_press.py",
    "routes/narrative_arc.py",
    "routes/partner_landing.py",
    "routes/competitive_intel.py",
]

# "285 US markets", "230+ data center markets", "300+ DCPI-scored markets".
# Deliberately NOT a bare \d+ search: every unrelated figure on these surfaces
# (0-100 scores, 4x/day, 7 ISOs, 369 GW) must stay legal, or the fence gets
# muted the first time it cries wolf.
_COUNT_THEN_NOUN = (
    r"\b\d{2,4}\+?[\s-]*"
    r"(?:(?:US|U\.S\.|power|data[\s-]?center|DCPI|scored|global|total)[\s-]+){0,3}"
    r"markets?\b"
)
# ★ The noun can also PRECEDE the count. routes/mcp_outreach_drafts.py shipped
#   "Power markets scored (DCPI): 285" — a label, then the number — which every
#   count-then-noun pattern reads straight past. It was still live on main
#   after the surfaces that read left-to-right had been fixed.
# Anchored to end-of-line: a stat line puts the number last. Without the
# anchor this also matched 'api/v1/markets/{slug}", timeout=20' — a URL and a
# keyword argument, neither of them a claim. A fence that fires on those gets
# switched off, which is the only failure mode worse than not having one.
_NOUN_THEN_COUNT = r"markets?\b[^\n]{0,30}?[:=]\s*\d{2,4}\+?\s*$"
_HARDCODED_COUNT = re.compile(
    f"(?:{_COUNT_THEN_NOUN})|(?:{_NOUN_THEN_COUNT})", re.I | re.M)

# Lines that are GUARDS against a retired literal, not publications of it.
_GUARD_MARKERS = ("NOT ILIKE", "re.compile(", "stale_markers")


def _publishable_lines(path):
    """Non-comment, non-guard source lines — what the module can actually emit.

    Comments are excluded for the reason tests/test_canon_placeholders_resolved.py
    excludes them: a fix's own prose quotes the retired literal to explain what
    was wrong, and that has to stay legal.
    """
    for i, line in enumerate(path.read_text().splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        if any(m in line for m in _GUARD_MARKERS):
            continue
        yield i, line


def _normalized_publishable_text(path):
    """Publishable text with the two seams a per-line scan cannot see.

    ★ Both were found by MUTATING an earlier version of this fence:
      1. Implicit string concatenation splits a claim across lines, so
         "285 US power " / "markets." reads as two innocent lines. Python
         joins them at parse time and the agent sees one sentence.
      2. Served HTML puts markup between the count and its noun
         ('<div class="stat-num">285</div>...<div ...>DCPI markets</div>'),
         which no whitespace-based pattern can bridge.

    Without these the fence passes on a literal that is live on the page — a
    guard reading a strict subset of what the module publishes.
    """
    text = "\n".join(
        "" if line.lstrip().startswith("#") or any(m in line for m in _GUARD_MARKERS)
        else line
        for line in path.read_text().splitlines()
    )
    text = re.sub(r"<[^>]{0,200}>", " ", text)      # markup -> space
    text = re.sub(r'"\s*\n\s*"', " ", text)         # stitch "..." / "..."
    text = re.sub(r"'\s*\n\s*'", " ", text)
    return text


@pytest.mark.parametrize("fname", _PUBLISHERS)
def test_no_hardcoded_dcpi_market_count(fname):
    """A market count on these surfaces must come from canon, not a keystroke."""
    path = _ROOT / fname
    offenders = [
        (i, line.strip()[:100])
        for i, line in _publishable_lines(path)
        if _HARDCODED_COUNT.search(line)
    ]
    assert not offenders, (
        f"{fname} publishes a hand-typed DCPI market count. Use "
        "{canon_markets} via canon_text(), or canonical_stats.markets_phrase() "
        "inside an f-string:\n"
        + "\n".join(f"  line {i}: {txt}" for i, txt in offenders)
    )

    stitched = _HARDCODED_COUNT.findall(_normalized_publishable_text(path))
    assert not stitched, (
        f"{fname} publishes a hand-typed DCPI market count once its string "
        f"literals are joined / its markup is rendered: {stitched}"
    )


def _canon_phrase():
    return canonical_stats.markets_phrase()


# ── the surfaces, actually RENDERED ────────────────────────────────────────
# Static structure cannot tell you what a byte-stream looks like: an f-string
# parses {canon_x} as an expression (NameError), and a plain string does not
# substitute at all. Both failures pass every check above. Render them.

def test_outreach_draft_stats_block_is_live():
    """The block is headed "Stats (live)"; the DCPI line was not."""
    import routes.mcp_outreach_drafts as mod

    draft = mod._draft_for_target({"name": "t", "url": "https://x/"}, 48, 20100)
    text = draft["markdown_block"]
    assert "{canon_" not in text, "unresolved placeholder in a registry submission"
    m = re.search(r"Power markets scored \(DCPI\):[ \t]*(\S+)", text)
    assert m, "the DCPI stat line vanished from the outreach draft"
    assert m.group(1) == _canon_phrase()

    long_, short_, tweet = mod._resolved_descs()
    for blurb in (long_, short_, tweet):
        assert "{canon_" not in blurb
        assert _canon_phrase() in blurb


def test_outreach_drafts_are_not_latched_at_import(monkeypatch):
    """★ Resolving canon at MODULE scope reads as canon-bound and is not.

    canon_text() runs once at import, so the value freezes for the life of the
    process — it satisfies the AST guard and drains the ledger entry while the
    surface keeps serving what the canon said at boot. These strings go to
    third-party registries, which republish them, so a frozen number outlives
    our next deploy on someone else's site.

    Asserting the value equals the canon phrase CANNOT catch this: both read
    the same in-process value. Move the canon and require the surface to follow.
    """
    import routes.mcp_outreach_drafts as mod

    monkeypatch.setattr(
        mod, "canon_text",
        lambda t: re.sub(r"\{canon_[a-z_]+\}", "CANON_MOVED", t) if t else t)
    draft = mod._draft_for_target({"name": "t", "url": "https://x/"}, 48, 20100)
    assert "CANON_MOVED" in draft["markdown_block"], (
        "the registry draft ignored a canon change — it is latched at import"
    )
    assert "CANON_MOVED" in draft["tweet_announcement"]


def test_partner_landing_bullet_matches_its_derived_neighbours():
    import routes.partner_landing as pl

    # perplexity is the partner whose value_bullets carry the DCPI market
    # count; asserting on a page that never had the bullet would pass forever.
    html = pl._render_partner_page("perplexity", pl._PARTNERS["perplexity"])
    assert "@@CANON" not in html, "an unresolved canon token reached the page"
    assert "{canon_" not in html and "{_CANON" not in html
    assert f"{_canon_phrase()} markets, US + international" in html, (
        "the DCPI bullet lost its derived count"
    )


def test_partner_landing_iso_count_is_derived():
    """The live-strip named ISOs and published the GRID OPERATOR count.

    10 is the operator total (the US ISOs plus TVA, BPA and IESO); there are 7
    live US ISOs, so the strip over-claimed by three the metric it named. The
    repo already banned this shape — tests/test_canonical_counts_drift.py's
    isos_non_canonical — but this file sat in KNOWN_STALE_COUNT_DEBT, so the
    ban was recorded rather than enforced here.
    """
    import routes.partner_landing as pl

    isos = ai_surface_canon.canon_nums()["{canon_isos}"]
    html = pl._render_partner_page("cohere", pl._PARTNERS["cohere"])
    m = re.search(r"markets\s*·\s*([^<]*?)\s*ISOs", html)
    assert m, "the ISO clause vanished from the live-strip"
    assert m.group(1) == isos


def test_partner_landing_is_off_the_iso_debt_register():
    """A fix that leaves its debt entry behind re-permits the defect."""
    drift = (_ROOT / "tests/test_canonical_counts_drift.py").read_text()
    m = re.search(r"'routes/partner_landing\.py':\s*\{([^}]*)\}", drift)
    if m:
        assert "isos_non_canonical" not in m.group(1), (
            "routes/partner_landing.py still claims isos_non_canonical as known "
            "debt, so the repo-wide fence stays disarmed for this file"
        )


def test_partner_pages_are_not_latched_at_import(monkeypatch):
    """Same import-time latch as the outreach drafts, on served HTML.

    Measured live before this was fixed, same host and second:
    /api/v1/canon/phrases and /partners/<slug> gave DIFFERENT facility counts
    from one process. Comparing the page against the canon phrase cannot see
    that — both read the same latched value — so move the canon and require
    every partner page to follow.
    """
    import routes.partner_landing as pl

    monkeypatch.setattr(
        pl, "canon_text",
        lambda t: re.sub(r"\{canon_[a-z_]+\}", "CANON_MOVED", t) if t else t)
    for slug in list(pl._PARTNERS)[:3]:
        html = pl._render_partner_page(slug, pl._PARTNERS[slug])
        assert "CANON_MOVED" in html, (
            f"/partners/{slug} ignored a canon change — it is latched at import"
        )


def test_auto_press_body_is_derived():
    """This body ships to LinkedIn / X / Bluesky unreviewed."""
    import routes.dcpi_auto_press as ap

    shift = {
        "slug": "cheyenne", "name": "Cheyenne", "iso": "WECC", "delta": 18.4,
        "prior_excess": 52.0, "new_excess": 70.4,
        "prior_verdict": "CAUTION", "new_verdict": "BUILD",
        "is_verdict_flip": True,
    }
    body = ap._draft_press_release(shift)["body"]
    assert "{_mkts}" not in body and "{canon_" not in body
    assert f"DCPI scores {_canon_phrase()} data center markets" in body


def test_auto_press_reads_as_a_sentence_when_canon_is_unreadable(monkeypatch):
    """Fail-open must yield a COUNT-FREE sentence, never a broken one.

    A missing number is visible; a retired one is not — but a stray double
    space in a published press release is a typo we shipped, so the fail-open
    path is rendered here too.
    """
    import canonical_stats as cs
    import routes.dcpi_auto_press as ap

    monkeypatch.setattr(cs, "markets_phrase", lambda: "")
    shift = {
        "slug": "cheyenne", "name": "Cheyenne", "iso": "WECC", "delta": 18.4,
        "prior_excess": 52.0, "new_excess": 70.4,
        "prior_verdict": "CAUTION", "new_verdict": "BUILD",
        "is_verdict_flip": True,
    }
    body = ap._draft_press_release(shift)["body"]
    assert "DCPI scores data center markets" in body
    assert "  " not in body, "fail-open left a double space in published copy"


def test_narrative_arc_fallback_is_derived():
    """The last-resort arc publishes exactly when everything else failed."""
    import routes.narrative_arc as na

    assert na._canon_markets() == _canon_phrase()
    src = (_ROOT / "routes/narrative_arc.py").read_text()
    assert "Real-time DCPI for {_m} markets" in src


def test_narrative_arc_fallback_reads_when_canon_is_unreadable(monkeypatch):
    import canonical_stats as cs
    import routes.narrative_arc as na

    monkeypatch.setattr(cs, "markets_phrase", lambda: "")
    assert na._canon_markets() == ""


# A value canon will never produce, so a string that carries it can only have
# come through the resolver. Comparing against the real phrase cannot do this:
# a hardcoded literal that happens to EQUAL the live phrase satisfies it, which
# is how the competitive_intel counts survived every previous check.
_SENTINEL = "<<CANON>>"


def _sentinel_canon_text(s):
    return re.sub(r"\{canon_[a-z_]+\}", _SENTINEL, s) if s else s


def _resolved_with_sentinel(ci):
    real = ci.canon_text
    ci.canon_text = _sentinel_canon_text
    try:
        return {d["key"]: d["value"] for d in ci._resolved_differentiators()}
    finally:
        ci.canon_text = real


def test_competitive_intel_market_counts_are_derived():
    """These two matched canon by luck, which is the quiet version of the bug.

    The DCPI differentiator sat two entries above a `facilities` claim that
    already resolved per request — and that entry carries a comment recording
    the exact failure this one was set up for: a value frozen at import,
    drifting away from the resolver the same response uses elsewhere.
    """
    import routes.competitive_intel as ci

    phrase = canonical_stats.markets_phrase()

    # module-level literal must hold no copy at all — a direct reader of
    # _DCHUB_DIFFERENTIATORS gets nothing rather than something stale
    entry = next(d for d in ci._DCHUB_DIFFERENTIATORS
                 if d["key"] == "proprietary_indices")
    assert entry["value"] == "", (
        "the DCPI differentiator carries copy at module level again, which "
        "freezes it at import"
    )

    resolved = {d["key"]: d["value"] for d in ci._resolved_differentiators()}
    dcpi = resolved["proprietary_indices"]
    assert f"scores {phrase} markets" in dcpi, f"not derived: {dcpi[:120]!r}"
    assert "{canon_" not in dcpi
    # the gas token is filled by resolve_gas_copy AFTER the count substitution;
    # if the ordering ever flips, the token ships raw to an agent
    assert "@@" not in dcpi, f"unresolved gas token: {dcpi[:120]!r}"

    # ★ The assertion above cannot tell DERIVED from LUCKY. The literal these
    #   two strings carried was "300+", which is what canon resolves to today,
    #   so a hardcoded copy satisfies every check that compares against the
    #   canon phrase — mutation testing caught exactly that. Drive the resolver
    #   to a value canon never produces and require the surface to follow.
    assert _SENTINEL in _resolved_with_sentinel(ci)["proprietary_indices"], (
        "the DCPI differentiator ignores the resolver — it matched canon by "
        "coincidence, not by derivation"
    )


def test_competitive_intel_media_draft_is_derived():
    flask = pytest.importorskip("flask")
    import routes.competitive_intel as ci

    app = flask.Flask(__name__)
    real_guard = ci._admin_guard
    ci._admin_guard = lambda: None          # exercising copy, not auth
    try:
        with app.test_request_context("/api/v1/competitive/media-drafts"):
            out = ci.media_drafts()
            resp = out[0] if isinstance(out, tuple) else out
            txt = resp.get_data(as_text=True)
    finally:
        ci._admin_guard = real_guard

    phrase = canonical_stats.markets_phrase()
    assert f"report scores {phrase} markets" in txt, "media draft not derived"
    assert "{canon_" not in txt

    # ★ Same coincidence trap as above — re-render with the resolver forced to
    #   a value canon never produces.
    ci._admin_guard = lambda: None
    real_canon_text = ci.canon_text
    ci.canon_text = _sentinel_canon_text
    try:
        with app.test_request_context("/api/v1/competitive/media-drafts"):
            out = ci.media_drafts()
            resp = out[0] if isinstance(out, tuple) else out
            forced = resp.get_data(as_text=True)
    finally:
        ci.canon_text = real_canon_text
        ci._admin_guard = real_guard
    assert _SENTINEL in forced, (
        "the media draft ignores the resolver — it matched canon by "
        "coincidence, not by derivation"
    )


def test_canon_markets_placeholder_still_resolves():
    """These surfaces all lean on one placeholder; if it stops resolving they
    serve the raw token, which is worse than the number it replaced."""
    val = ai_surface_canon.canon_nums().get("{canon_markets}")
    assert val and re.fullmatch(r"[\d,]+\+", val), f"bad phrase shape: {val!r}"
    assert "{canon_" not in ai_surface_canon.canon_text("x {canon_markets} y")
