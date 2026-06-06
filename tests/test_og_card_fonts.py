"""OG-card font regression guard (2026-06-06).

FENCES the "empty social cards" bug. routes/og_cards.py renders 1200x630
LinkedIn/X cards with PIL. _font() used to probe only SYSTEM font paths
(/usr/share/fonts/.../dejavu, macOS Helvetica). Railway's Nixpacks image has
neither, so PIL silently fell back to ImageFont.load_default() — a ~10px bitmap
font that IGNORES the requested size. Every headline/number/label rendered tiny
and the cards looked ~80% empty. It was INVISIBLE in dev (macOS has Helvetica),
so only prod was broken.

Durable fix: the DejaVu faces are now BUNDLED in routes/fonts/ and loaded first.
These tests fail pre-merge if (a) a bundled face goes missing, or (b) _font()
ever stops honoring the requested pixel size (i.e. falls back to load_default).

Pure local rendering — no DB, no network. Runs in pre-merge.yml (`pytest tests/`).
"""
import os
import pytest

FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "routes", "fonts")
REQUIRED_FACES = ["DejaVuSans-Bold.ttf", "DejaVuSans.ttf", "DejaVuSansMono-Bold.ttf"]


def test_bundled_fonts_present():
    """The TTFs must ship in the repo — the whole fix depends on them."""
    for face in REQUIRED_FACES:
        path = os.path.join(FONT_DIR, face)
        assert os.path.isfile(path), f"bundled font missing: {path}"
        assert os.path.getsize(path) > 50_000, f"bundled font looks truncated: {path}"


def test_font_honors_size_not_load_default():
    """_font()/_mono() must render at the REQUESTED size. load_default()'s
    bitmap glyph is ~10px tall regardless of the size argument — that's the
    exact failure mode that made every card look empty. A 120px font must
    produce a glyph far taller than the bitmap default."""
    pytest.importorskip("PIL")
    from routes import og_cards
    for getter in (lambda s: og_cards._font(s, bold=True),
                   lambda s: og_cards._font(s, bold=False),
                   og_cards._mono):
        small = getter(40)
        big = getter(120)
        h_small = small.getbbox("69.5")[3] - small.getbbox("69.5")[1]
        h_big = big.getbbox("69.5")[3] - big.getbbox("69.5")[1]
        assert h_big > 60, (f"font not honoring size (got {h_big}px at 120 — "
                            f"load_default fallback?). Bundled font missing on this host.")
        assert h_big > h_small * 1.8, "font size argument is being ignored"
    # And the loud-fallback flag must not have tripped during the calls above.
    assert og_cards._FONT_FELL_BACK is False, "og_cards fell back to load_default()"


def test_all_four_styles_render_without_error():
    """Each card style must produce a 1200x630 image from a representative
    press-release dict — catches a renderer that crashes (→ blank fallback)."""
    pytest.importorskip("PIL")
    from routes import og_cards
    pr = {
        "slug": "test-slug",
        "title": "Blackstone's $30B AirTrunk Acquisition Headlines $62.5B Deal Wave",
        "subheadline": "Six major transactions in 24 hours; Cheyenne leads at 69.5.",
        "topic": "data center M&A",
        "date": __import__("datetime").date(2026, 6, 6),
        "signals": {"top_build_markets": [
            {"market": "Cheyenne", "name": "Cheyenne", "slug": "cheyenne",
             "score": 69.5, "excess": 69.5, "constraint": 31.2, "verdict": "BUILD"},
            {"market": "Montréal", "name": "Montréal", "slug": "montreal",
             "score": 65.2, "excess": 65.2, "constraint": 34.0, "verdict": "BUILD"},
        ]},
    }
    for style, fn in og_cards.STYLE_MAP.items():
        img = fn(pr)
        assert img.size == (og_cards.W, og_cards.H), f"{style} wrong size: {img.size}"
