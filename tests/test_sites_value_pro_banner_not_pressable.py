#!/usr/bin/env python3
"""The PRO banner's non-controls must not wear the page's button vocabulary.

NO NETWORK, NO DB — the module's HTML constants are parsed directly.

CONTEXT — #4277 (merged 2026-09-09 05:49Z) already refuted the obvious reading
of this ask, and that finding stands: verified live on dchub.cloud 2026-09-09,
NOTHING on /sites/value has `cursor:pointer` without being a real control, and
the form labels already WRAP their inputs so `<label for=…>` would be a no-op.
The banner is not "fake-clickable" in the pointer sense and never was.

What #4277 actually fixed was AFFORDANCE — "the banner looked like a card …
flattened to a left rule, still an unmistakable confirmation, no longer shaped
like something you press." It scoped that to _PRO_OK_BANNER, the green banner
only PAID users see, and left the two elements that FREE visitors see.

Measured live 2026-09-09 by walking the rendered page and computing styles,
three elements were filled + padded + bold + rounded — the exact vocabulary
this design uses for buttons — while being inert:

    🔒 PRO + DEVELOPER + ENTERPRISE ONLY   rgba(0,0,0,0.35)  radius 999px
    PRO+ PREMIUM                           rgb(14,165,233)   radius 4px
    1.000×                                 rgb(31,41,55)     radius 4px

The first two are the PRO banner and are fixed here. The third (`#r-mult`) is a
live value readout that changes as the readiness boxes toggle — same shape
class, but it is data, not a claim of interactivity, and it was not in scope.
It is named here so the next person does not have to re-measure it.

A 999px pill is the strongest "press me" signal in the set, which is why the
rule below rejects a pill outright rather than only asking for a cursor.

THE LIMIT: this pins the markup, which is ours. Whether Clarity's dead-click
count on /sites/value falls is measured in Clarity after deploy, not here —
and this session could not read Clarity, so whether dead clicks persisted
after #4277 shipped at 05:49Z is UNVERIFIED.
"""
import pathlib
import re
import sys
from html.parser import HTMLParser

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import routes.site_valuation_engine as sve  # noqa: E402

CONTROLS = {"a", "button", "input", "select", "textarea", "label", "summary"}


def _decls(style: str) -> dict:
    out = {}
    for part in style.split(";"):
        if ":" in part:
            k, _, v = part.partition(":")
            out[k.strip().lower()] = v.strip().lower()
    return out


class _Shapes(HTMLParser):
    """Collect elements that wear the page's button vocabulary."""

    def __init__(self):
        super().__init__()
        self.hits = []
        self._open = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self._open.append((tag, a.get("style", "")))

    def handle_endtag(self, tag):
        if self._open:
            self._open.pop()

    def handle_data(self, data):
        text = data.strip()
        if not text or not self._open:
            return
        tag, style = self._open[-1]
        d = _decls(style)
        bg = d.get("background", "") or d.get("background-color", "")
        filled = bool(bg) and "transparent" not in bg and "gradient" not in bg
        padded = "padding" in d
        rounded = "border-radius" in d
        try:
            bold = int(d.get("font-weight", "400")) >= 700
        except ValueError:
            bold = False
        if filled and padded and rounded and bold:
            self.hits.append({
                "tag": tag, "text": text[:60],
                "radius": d.get("border-radius", ""),
                "cursor": d.get("cursor", ""),
                "is_control": tag in CONTROLS,
            })


def _button_shaped(html: str):
    p = _Shapes()
    p.feed(html)
    return [h for h in p.hits if not h["is_control"]]


def _radius_px(r: str) -> float:
    m = re.match(r"^([\d.]+)px$", r.strip())
    return float(m.group(1)) if m else 0.0


# ── the rule ─────────────────────────────────────────────────────────────

def _assert_not_pressable(html: str, where: str):
    for h in _button_shaped(html):
        assert _radius_px(h["radius"]) < 100, (
            f"{where}: {h['text']!r} is filled, padded, bold and a "
            f"{h['radius']} PILL but is a <{h['tag']}>, not a control — that "
            f"is the strongest press affordance on the page")
        assert h["cursor"] == "default", (
            f"{where}: {h['text']!r} wears the button vocabulary (filled + "
            f"padded + bold + rounded) on a <{h['tag']}> that does nothing, "
            f"and does not declare cursor:default to say so")


def test_pro_hero_banner_has_no_pressable_looking_non_controls():
    """The banner FREE and STARTER visitors see — the majority of traffic."""
    _assert_not_pressable(sve._PRO_HERO_BANNER, "_PRO_HERO_BANNER")


def test_page_kicker_badge_is_not_pressable_looking():
    """`PRO+ PREMIUM` in the page kicker, above the fold for every visitor."""
    kicker = re.search(r'<div class="kicker">.*?</div>',
                       sve._PAGE_HTML, re.S)
    assert kicker, "kicker block not found — did the page header move?"
    _assert_not_pressable(kicker.group(0), "kicker")


def test_pro_ok_banner_stays_flat():
    """#4277's fix must not regress: no filled pressable-looking non-control."""
    _assert_not_pressable(sve._PRO_OK_BANNER, "_PRO_OK_BANNER")


# ── the detector is not vacuous ──────────────────────────────────────────

def test_detector_actually_finds_a_button_shaped_non_control():
    """Guard the guard: on the pre-fix markup it must FIRE, not pass empty."""
    prefix = ('<div style="display:inline-block;background:rgba(0,0,0,0.35);'
              'color:#fff;font-weight:800;padding:6px 12px;'
              'border-radius:999px;">LOCKED</div>')
    found = _button_shaped(prefix)
    assert len(found) == 1, f"detector saw {found!r}"
    with pytest.raises(AssertionError, match="PILL"):
        _assert_not_pressable(prefix, "synthetic")


def test_detector_does_not_flag_a_real_control():
    ok = ('<a href="/pricing" style="background:#fff;color:#0369A1;'
          'font-weight:700;padding:14px 28px;border-radius:8px;">Buy</a>')
    assert _button_shaped(ok) == []
