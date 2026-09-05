"""The nine partner landing pages must not type their own numbers.

These are the pages the AI-lab outreach emails link to. On 2026-08-29 three
value_bullets carried hardcoded figures while their neighbours on the SAME
pages were canon-derived:

    "4,000+ tracked M&A deals"    over-claim, retired 2026-07-17 (rows, not deals)
    "21,405+ facilities"          over-claim (canon 18,500+)
    "13,000+ global facilities"   UNDER-claim, ~5k low

Half-canonised is how a wrong number survives: it sits beside a derived one and
looks equally trustworthy. A partner who checked the emailed claim landed here
and found a *different* wrong number.
"""
import ast
import re

SRC = open("routes/partner_landing.py", encoding="utf-8").read()


def _code_only():
    """Source with comments stripped — this module's header quotes the retired
    literals on purpose, and a whole-file grep would fail on the fix."""
    return "\n".join(l for l in SRC.split("\n") if not l.strip().startswith("#"))


def test_no_hardcoded_entity_counts_in_value_bullets():
    code = _code_only()
    for lit in ('"4,000+', '"21,405+', '"13,000+'):
        assert lit not in code, f"{lit} is typed, not derived"


def test_every_entity_claim_is_canon_derived():
    """Entity claims come from canon — checked by BEHAVIOUR, not by source text.

    ★2026-09-05: this test used to assert the literal source lines
        _CANON_DEALS = canon_text("{canon_deals}")
        _CANON_MKTS  = canon_text("{canon_markets}")
    were present. That pinned the wrong thing. A module-scope canon_text()
    call resolves ONCE, at import, so those lines are an import-time LATCH:
    the page served whatever the canon said at boot, and diverged from
    /api/v1/canon/phrases in the same process (measured live, same second).
    dchub-backend #3831 named the mechanism while retiring a page over it.

    Pinning the source line meant this guard REQUIRED the defect and would
    have failed the fix. It now asserts what it always meant: the claims are
    canon-derived at render time. The structural sibling below still catches
    typed counts, and tests/test_canon_resolved_per_request.py fences the
    latch itself.
    """
    import re as _re
    import routes.partner_landing as pl

    # the copy carries canon TOKENS, not baked-in values
    code = _code_only()
    assert "@@CANON_DEALS@@ tracked M&A deals" in code
    assert "@@CANON_FAC@@ global facilities" in code

    # and the rendered page carries resolved canon values
    html = pl._render_partner_page("perplexity", pl._PARTNERS["perplexity"])
    assert "@@CANON" not in html, "an unresolved canon token reached the page"
    assert "{canon_" not in html

    # ...which FOLLOW the canon rather than a boot-time snapshot
    real = pl.canon_text
    pl.canon_text = lambda t: _re.sub(r"\{canon_[a-z_]+\}", "CANON_MOVED", t) if t else t
    try:
        moved = pl._render_partner_page("perplexity", pl._PARTNERS["perplexity"])
    finally:
        pl.canon_text = real
    assert "CANON_MOVED" in moved, (
        "the page ignored a canon change — the claims are latched at import"
    )


def test_no_bare_thousands_figure_next_to_a_fenced_noun():
    """Catches the next one. Any 4-5 digit literal immediately followed by
    facilities/deals/markets inside a string is a typed entity count."""
    bad = []
    for node in ast.walk(ast.parse(SRC)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            m = re.search(r"\b\d{1,3},\d{3}\+?\s*(?:global\s+|tracked\s+|DCPI\s+)*"
                          r"(facilities|deals|markets|M&A)", node.value, re.I)
            if m:
                bad.append(f"line {node.lineno}: {m.group(0)}")
    assert not bad, ("typed entity counts found — derive from canon_text():\n  "
                     + "\n  ".join(bad))
