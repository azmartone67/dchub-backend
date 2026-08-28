"""tests/test_sponsor_creative_intake.py — a sponsor's creative is checked at
the door, and the published spec is the check (2026-08-28).

WHAT WAS TRUE BEFORE THIS. `hero_html` was TEXT NOT NULL guarded by one rule:
non-empty after .strip(). No length cap, no tag allowlist, no image policy, no
link policy. `sponsor_module_html` then interpolated it RAW into the page. The
answer to "what do I send you?" was "email us some HTML and we will paste it",
which is why no spec sheet existed to send a prospect.

★ THE INVARIANT THIS FILE PROTECTS ABOVE ALL OTHERS: validation lives at the
  POST, never in routes/sponsor_render.py. The renderer is fail-soft by
  construction — every failure path returns ''. A check there would silently
  drop a PAYING sponsor's block off a live page rather than reject a bad
  submission while a human is waiting for the answer.

★ AND: our own live house creative must keep passing. A cap tightened without
  looking at what is actually running is how the first thing the new rule
  rejects is the thing already on the page.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CREATIVE = ROOT / "routes" / "sponsor_creative.py"
RENDER = ROOT / "routes" / "sponsor_render.py"
SPONSORSHIPS = ROOT / "routes" / "sponsorships.py"

from routes import sponsor_creative as sc

GOOD = {
    "sponsor_name": "Acme Power Systems",
    "hero_html": "Acme builds medium-voltage switchgear for hyperscale campuses. "
                 "Lead times under 30 weeks in ERCOT and PJM.",
    "link_url": "https://acme.example.com/datacenters",
}


def _v(**over):
    return sc.validate_creative({**GOOD, **over})


def _errs(**over):
    return " ".join(_v(**over)["errors"])


# ── the control: a validator that rejects everything proves nothing ──
def test_ordinary_prose_is_accepted():
    res = _v()
    assert res["ok"] is True, res["errors"]
    assert res["plain_chars"] == len(GOOD["hero_html"])


def test_light_inline_emphasis_is_accepted():
    assert _v(hero_html="Acme builds <b>switchgear</b>.<br>Lead times "
                        "under <em>30 weeks</em>.")["ok"] is True


# ── tags ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("tag_html", [
    '<script>alert(1)</script>',
    '<iframe src="https://x.example"></iframe>',
    '<style>body{display:none}</style>',
    '<svg onload="x()"></svg>',
    '<form action="/x"><input name="p"></form>',
    '<object data="x"></object>',
    '<div>hello</div>',
])
def test_structural_and_active_tags_are_rejected(tag_html):
    res = _v(hero_html="Acme builds switchgear. " + tag_html)
    assert res["ok"] is False
    assert "not allowed" in " ".join(res["errors"])


def test_an_anchor_is_rejected_and_says_why_one_link_is_the_product():
    """★ An <a> inside hero_html would render on the HTML surface WITHOUT
    rel="sponsored nofollow", outside the click counter, pointing anywhere."""
    e = _errs(hero_html='Acme builds <a href="https://x.example">switchgear</a>.')
    assert "not allowed" in e
    assert "one link" in e and "sponsored nofollow" in e


def test_images_are_rejected_with_the_surface_reason():
    """The reason is not taste. Three of four surfaces are plain text or JSON,
    including the root domain AI engines cite."""
    e = _errs(hero_html='Acme switchgear. <img src="https://x.example/a.png">')
    assert "Images are not accepted" in e
    assert "four" in e


@pytest.mark.parametrize("attr_html", [
    '<b style="position:fixed;top:0">x</b>',
    '<em class="dchub-official">x</em>',
    '<b onclick="x()">x</b>',
])
def test_attributes_are_rejected_on_permitted_tags(attr_html):
    """style lets a creative restyle the page around it; class lets it
    impersonate our own components; on* is script."""
    res = _v(hero_html="Acme switchgear. " + attr_html)
    assert res["ok"] is False
    assert "attributes" in " ".join(res["errors"])


def test_unbalanced_tags_are_rejected():
    e = _errs(hero_html="Acme builds <b>switchgear for campuses.")
    assert "unbalanced" in e


def test_markup_with_no_readable_text_is_rejected():
    """Blank on the three surfaces that strip markup — which includes every
    surface Product 2 is sold against."""
    e = _errs(hero_html="<br><b></b><br>")
    assert "no readable text" in e


@pytest.mark.parametrize("payload", [
    'Click <b>here</b> javascript:alert(1)',
    'See data:text/html;base64,PHNjcmlwdD4=',
])
def test_script_and_data_urls_are_rejected(payload):
    assert _v(hero_html=payload)["ok"] is False


# ── lengths, at and over the boundary ────────────────────────────────
def test_readable_text_at_the_published_limit_is_accepted():
    assert _v(hero_html="a" * sc.MAX_PLAIN_CHARS)["ok"] is True


def test_readable_text_one_over_the_limit_is_rejected():
    res = _v(hero_html="a" * (sc.MAX_PLAIN_CHARS + 1))
    assert res["ok"] is False
    assert str(sc.MAX_PLAIN_CHARS) in " ".join(res["errors"])


def test_markup_cannot_smuggle_past_the_readable_cap():
    """The raw cap exists so a creative cannot be a page's worth of tags around
    400 readable characters."""
    body = "<b>a</b>" * 200          # 200 readable chars, 1600 raw
    res = _v(hero_html=body)
    assert res["ok"] is False
    assert str(sc.MAX_RAW_CHARS) in " ".join(res["errors"])


def test_sponsor_name_limits():
    assert _v(sponsor_name="A" * (sc.MAX_SPONSOR_NAME_CHARS + 1))["ok"] is False
    assert "markup" in _errs(sponsor_name="Acme <b>Power</b>")
    assert _v(sponsor_name="Acme\nPower")["ok"] is False


# ── the one link ─────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", [
    "http://acme.example.com",
    "javascript:alert(1)",
    "//acme.example.com",
    "acme.example.com",
])
def test_link_must_be_https(bad):
    res = _v(link_url=bad)
    assert res["ok"] is False


def test_link_with_embedded_credentials_is_rejected():
    e = _errs(link_url="https://user:pw@acme.example.com/x")
    assert "phishing" in e


def test_optional_fields_are_shape_checked():
    assert sc.validate_creative({**GOOD, "sponsor_email": "nope"})["ok"] is False
    assert sc.validate_creative({**GOOD, "week_of": "09/01/2026"})["ok"] is False
    assert sc.validate_creative({**GOOD, "price_cents": -1})["ok"] is False
    assert sc.validate_creative({**GOOD, "sponsor_email": "a@b.co",
                                 "week_of": "2026-09-01",
                                 "price_cents": 150000})["ok"] is True


# ── the caps are set against what we actually run ────────────────────
# Captured from the live /api/v1/sponsorships/active house placement on
# 2026-08-28: 315 readable characters, 22-character name. Pinned so a future
# cap change that would reject the block already on the page fails here first.
LIVE_HOUSE_CREATIVE = {
    "sponsor_name": "DC Hub House Placement",
    "hero_html": (
        "This sponsorship slot is currently UNSOLD and is running as a DC Hub "
        "house placement while we test how AI engines reproduce sponsorship "
        "labelling. No advertiser has paid for this position and no third party "
        "is being promoted. Sponsorship inventory, rates and audience figures "
        "are published at dchub.cloud/advertise."),
    "link_url": "https://dchub.cloud/advertise",
}


def test_the_creative_currently_live_still_passes_the_published_spec():
    res = sc.validate_creative(LIVE_HOUSE_CREATIVE)
    assert res["ok"] is True, res["errors"]
    assert res["plain_chars"] == 315, (
        "the pinned live creative changed shape — re-measure before trusting "
        "this as a boundary case")


# ── the plain projection must match what actually ships ──────────────
@pytest.mark.parametrize("fragment", [
    "plain words",
    "<b>bold</b> and <em>italic</em>",
    "line<br>break",
    "&amp; entity &lt;escaped&gt;",
    "  collapsed   whitespace  ",
    "<b>unclosed",
])
def test_plain_projection_matches_the_renderer(fragment):
    """★ The readable-character cap is measured on this projection. If it drifts
    from routes.sponsor_render._plain — the one that actually ships — we cap a
    length nobody ever sees."""
    from routes.sponsor_render import _plain
    assert sc.plain_text(fragment) == _plain(fragment)


# ── where the check lives ────────────────────────────────────────────
def _imported_modules(path):
    tree = ast.parse(path.read_text())
    mods = [n.module for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module]
    assert mods, f"no imports parsed from {path} — vacuous assertion"
    return mods


def test_the_post_route_validates():
    assert "routes.sponsor_creative" in _imported_modules(SPONSORSHIPS)
    tree = ast.parse(SPONSORSHIPS.read_text())
    post = [n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "queue_sponsorship"]
    assert len(post) == 1
    calls = [n for n in ast.walk(post[0])
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "validate_creative"]
    assert calls, "POST /api/v1/sponsorships does not validate the creative"


def test_the_renderer_does_not_validate():
    """★★★ THE INVARIANT. sponsor_render returns '' on every failure path, so a
    check moved there would silently drop a paying sponsor's block off a live
    page instead of rejecting a bad submission at the door."""
    assert "routes.sponsor_creative" not in _imported_modules(RENDER)
    assert "sponsor_creative" not in RENDER.read_text(), (
        "validation must not reach the fail-soft renderer")


def test_the_spec_is_public_and_quotes_no_prices():
    """/advertise is the one rate card. A second surface carrying prices is the
    thing an agency catches first."""
    tree = ast.parse(SPONSORSHIPS.read_text())
    consts = [n.value for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    assert consts, "no string constants parsed — vacuous assertion"
    assert "/api/v1/sponsorships/creative-spec" in consts

    import json
    body = json.dumps(sc.spec())
    for price in ("1,500", "2,500", "3,000", "5,000", "6,500", "$"):
        assert price not in body, f"the creative spec quotes a price: {price}"


def test_the_published_spec_reports_the_enforced_numbers():
    """A spec sheet maintained separately from the validator drifts on its
    first edit. Both must read the same constants."""
    s = sc.spec()["what_to_send"]
    assert s["hero_html"]["max_readable_chars"] == sc.MAX_PLAIN_CHARS
    assert s["hero_html"]["max_chars_including_markup"] == sc.MAX_RAW_CHARS
    assert s["hero_html"]["allowed_tags"] == list(sc.ALLOWED_TAGS)
    assert s["sponsor_name"]["max_chars"] == sc.MAX_SPONSOR_NAME_CHARS
    assert s["link_url"]["max_chars"] == sc.MAX_LINK_CHARS
