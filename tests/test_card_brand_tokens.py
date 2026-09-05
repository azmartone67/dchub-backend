"""The cards must render in dchub.cloud's OWN design system (2026-09-05).

Operator, on seeing a generated card: "doesn't look anything like our dchub font
and colors." Measured, that was not a matter of taste — the card renderer and
the website shared NOTHING:

    token      dchub.cloud            routes/og_cards.py (before)
    display    Instrument Sans        DejaVu Sans MONO      <- a terminal face
    mono       JetBrains Mono         DejaVu Sans Mono
    ground     #0a0a0f near-black     #0f172a slate-900 navy
    accent     #6366f1 -> #a855f7     #38bdf8 sky-400 cyan
    text       #f5f5f7                #f1f5f9 slate-100

Not one value agreed, and `routes/fonts/` bundled only DejaVu faces, so the
generator could not set the brand typeface even if asked.

These tests load the REAL fonts and read the REAL constants — they fail on a
behaviour regression, not on an edited comment.
"""
import datetime
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT_DIR = os.path.join(REPO, "routes", "fonts")

pytest.importorskip("PIL")

# The site's published custom properties, as RGB.
PUBLISHED = {
    "BG":     ((10, 10, 15),     "--bg #0a0a0f"),
    "PANEL":  ((19, 19, 25),     "--surface #131319"),
    "INDIGO": ((99, 102, 241),   "--indigo #6366f1"),
    "VIOLET": ((168, 85, 247),   "--violet #a855f7"),
    "TEXT":   ((245, 245, 247),  "--text #f5f5f7"),
    "MUTED":  ((161, 161, 170),  "--text-dim #a1a1aa"),
    "DIM":    ((113, 113, 122),  "--text-faint #71717a"),
}

BRAND_FACES = [
    "InstrumentSans-Bold.ttf", "InstrumentSans-SemiBold.ttf",
    "InstrumentSans-Medium.ttf", "InstrumentSans-Regular.ttf",
    "JetBrainsMono-Bold.ttf", "JetBrainsMono-Regular.ttf",
]


def test_the_brand_faces_are_bundled():
    """The whole fix depends on these shipping — Railway has no system fonts."""
    for face in BRAND_FACES:
        p = os.path.join(FONT_DIR, face)
        assert os.path.isfile(p), f"brand face missing: {face}"
        assert os.path.getsize(p) > 40_000, f"{face} looks truncated"


def test_the_display_face_is_the_sites_face_not_a_terminal_font():
    """A MONO face setting a headline is the single loudest 'made by a script'
    signal. Loads the font and asks it its own name."""
    from routes import og_cards as o
    for bold in (True, False):
        fam, _sub = o._font(72, bold=bold).getname()
        assert fam == "Instrument Sans", (
            f"headlines render in {fam!r}, not the site's Instrument Sans")
        assert "Mono" not in fam, f"a monospace face is setting display type: {fam!r}"


def test_the_mono_face_is_jetbrains_mono():
    from routes import og_cards as o
    fam, _sub = o._mono(24).getname()
    assert fam == "JetBrains Mono", f"labels render in {fam!r}, not JetBrains Mono"


def test_no_font_fallback_was_triggered():
    """load_default() ignores the requested size — the 2026-06-06 empty-card
    bug. If this trips, a face is missing from the deploy."""
    from routes import og_cards as o
    o._font(120, bold=True); o._font(40, bold=False); o._mono(18)
    assert o._FONT_FELL_BACK is False, "og_cards fell back to load_default()"


@pytest.mark.parametrize("name", sorted(PUBLISHED))
def test_palette_matches_the_published_tokens(name):
    from routes import og_cards as o
    want, label = PUBLISHED[name]
    got = getattr(o, name)
    assert tuple(got) == want, f"{name} is {got}, expected {want} ({label})"


def test_the_legacy_aliases_point_at_brand_colours_not_tailwind():
    """~90 call sites still say PURPLE/CYAN. Repointing the aliases is what
    moves the whole fleet — if one drifts back to Tailwind, cards split into
    two palettes."""
    from routes import og_cards as o
    assert tuple(o.PURPLE) == (99, 102, 241)
    assert tuple(o.PURPLE_LT) == (168, 85, 247)
    assert tuple(o.CYAN) == (99, 102, 241), "CYAN must not be cyan — the site has none"
    assert tuple(o.ACCENT) == (99, 102, 241)
    assert tuple(o.ACCENT2) == (168, 85, 247)
    # slate-900 / sky-400 must not survive anywhere in the palette
    for banned in ((15, 23, 42), (56, 189, 248), (124, 58, 237), (167, 139, 250)):
        for tok in ("BG", "PANEL", "PANEL_HI", "INDIGO", "VIOLET", "TEXT",
                    "MUTED", "DIM", "PURPLE", "PURPLE_LT", "CYAN"):
            assert tuple(getattr(o, tok)) != banned, f"{tok} is still Tailwind {banned}"


def test_verdict_colours_stay_outside_the_accent_ramp():
    """BUILD/CAUTION/AVOID are SEMANTIC. If a verdict shares the accent hue the
    card says 'brand' where it means 'verdict'."""
    from routes import og_cards as o
    for v in (o.GREEN, o.RED, o.AMBER):
        assert tuple(v) not in ((99, 102, 241), (168, 85, 247))


def test_gradient_text_actually_produces_a_gradient():
    """`.grad` is the site's signature, and a FLAT fill must fail this.

    ★ The first version of this test compared the single leftmost and rightmost
    lit pixels. Those are antialiased glyph EDGES at arbitrary opacities, so a
    flat indigo fill measured left=(38,39,93) right=(67,69,163) and "passed".
    Two fixes: sample only near-OPAQUE pixels, as windowed means; and assert the
    signal that only a real ramp has — indigo(99,102,241) -> violet(168,85,247)
    raises R while LOWERING G. A flat fill holds both constant."""
    from PIL import Image
    from routes import og_cards as o
    img = Image.new("RGB", (600, 140), (0, 0, 0))
    o._grad_text(img, (10, 10), "GRADIENT", o._font(90))
    px = img.load()
    opaque = [(x, px[x, y]) for x in range(600) for y in range(140)
              if sum(px[x, y]) > 380]                 # ~85% coverage and up
    assert len(opaque) > 500, "gradient text drew (almost) nothing"
    xs = sorted(p[0] for p in opaque)
    lo, hi = xs[len(xs) // 5], xs[len(xs) * 4 // 5]

    def band(pred):
        v = [c for x, c in opaque if pred(x)]
        return tuple(round(sum(c[i] for c in v) / len(v), 1) for i in range(3))

    left, right = band(lambda x: x <= lo), band(lambda x: x >= hi)
    assert right[0] - left[0] > 20, (
        f"red channel is flat across the fill: left={left} right={right}")
    assert left[1] - right[1] > 5, (
        f"green channel does not fall — this is a flat fill, not the "
        f"indigo->violet ramp: left={left} right={right}")


def _pr(slug, title="A headline that wraps onto more than one line for the card"):
    return {"slug": slug, "title": title,
            "subheadline": "20,500+ facilities · 170+ countries",
            "topic": "infrastructure", "date": datetime.date(2026, 9, 5),
            "signals": {}}


def test_evergreen_cards_are_not_date_stamped():
    """A date on evergreen marketing copy ages the card the moment it posts AND
    changes the bytes daily, which defeats the 7-day edge cache these are now
    served under (worker 4.9.53). Dated editorial keeps its date."""
    from routes import og_cards as o
    ever = o._draw_editorial(_pr("dyn-abc123"))
    dated = o._draw_editorial(_pr("2026-09-05-a-real-press-release"))
    band = (110, 40, 700, 90)          # the kicker row
    assert ever.crop(band).tobytes() != dated.crop(band).tobytes(), (
        "the evergreen and dated cards have an identical kicker row — the "
        "date is either on both or on neither")


def test_evergreen_card_is_byte_stable_across_days():
    """The same evergreen card rendered for two different dates must be
    identical, or the edge caches a new object every day."""
    from routes import og_cards as o
    a = _pr("dyn-stable"); a["date"] = datetime.date(2026, 9, 5)
    b = _pr("dyn-stable"); b["date"] = datetime.date(2027, 1, 30)
    assert o._draw_editorial(a).tobytes() == o._draw_editorial(b).tobytes(), \
        "an evergreen card still varies with the clock"


def test_every_style_still_renders_at_the_share_size():
    from routes import og_cards as o
    pr = _pr("test-slug")
    pr["signals"] = {"top_build_markets": [
        {"market": "Cheyenne", "name": "Cheyenne", "slug": "cheyenne",
         "score": 69.5, "excess": 69.5, "constraint": 31.2, "verdict": "BUILD"}]}
    for style, fn in o.STYLE_MAP.items():
        img = fn(pr)
        assert img.size == (o.W, o.H), f"{style} rendered {img.size}"
