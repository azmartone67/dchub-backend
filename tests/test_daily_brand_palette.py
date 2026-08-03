"""Guards on the /daily share-image renderer's palette + chart plate.

services/daily/render.py cannot be imported here: it pulls matplotlib and
numpy, and the pre-merge install line is `pytest requests flask pyyaml
psycopg2-binary psycopg[binary] Unidecode Pillow` — no matplotlib. An import
would raise at collection time and take the whole session down. So we read
the shipped source with `ast`, the same way the rest of this suite reaches
into main.py.

Two regressions these lock down, both of which shipped unnoticed:

1. Every theme except 'd' left the matplotlib axes facecolor at its DEFAULT,
   which is WHITE — so all nine share images rendered their bars on a
   full-bleed white rectangle punched out of the dark canvas.
2. The palette was lime/mint green on navy, matching nothing else we ship.

Both are invisible to any test that only checks "the PNG rendered without
raising", which is why they lasted.
"""
import ast
import re
from pathlib import Path

RENDER_PY = Path(__file__).resolve().parents[1] / "services" / "daily" / "render.py"

# DC Hub brand tokens — frontend static/dchub-brand.css. A colour that is not
# on this list does not belong in a published DC Hub image.
BRAND_HEXES = {
    "#0A0A0F", "#131319", "#1A1A22", "#FAFAFA", "#A1A1AA", "#71717A",
    "#818CF8", "#6366F1", "#A855F7", "#22D3EE", "#3B82F6", "#60A5FA",
    # theme-scoped darks/lights that stay inside the blue-violet ramp
    "#04040A", "#4F8FFF", "#C084FC", "#DCE7FF", "#7C89C4",
}

# Hue families that must never reappear: the old green/mint/magenta ramp.
BANNED_SUBSTRINGS = ("#6FE6", "#39FF", "#10B981", "#FF5EDC", "#E17CFF", "#1FF0B0")


def _module():
    src = RENDER_PY.read_text()
    return src, ast.parse(src)


def _resolve_dict(node, brand):
    """literal_eval a dict whose values may be BRAND["key"] subscripts."""
    out = {}
    for k, v in zip(node.keys, node.values):
        key = ast.literal_eval(k)
        if isinstance(v, ast.Dict):
            out[key] = _resolve_dict(v, brand)
        elif isinstance(v, ast.Subscript):
            out[key] = brand[ast.literal_eval(v.slice)]
        else:
            out[key] = ast.literal_eval(v)
    return out


def _top_level_dict(tree, name, brand=None):
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in stmt.targets
        ):
            return _resolve_dict(stmt.value, brand or {})
    raise AssertionError(f"{name} not found in {RENDER_PY.name}")


def test_palettes_are_brand_only_and_actually_parsed():
    src, tree = _module()
    brand = _top_level_dict(tree, "BRAND")
    pal = _top_level_dict(tree, "PAL", brand)

    # Anti-vacuous: an empty parse would satisfy every assertion below.
    assert len(brand) >= 8, f"BRAND resolved to only {len(brand)} tokens"
    assert set(pal) == {"a", "b", "c", "d"}, f"themes drifted: {sorted(pal)}"

    for theme, colors in pal.items():
        assert {"op", "uc", "ann", "bg", "ink"} <= set(colors), (
            f"theme {theme} is missing a required role: {sorted(colors)}")
        for role, value in colors.items():
            assert value.upper() in BRAND_HEXES, (
                f"theme {theme}.{role} = {value} is not a DC Hub brand token")

    # The three status colours must stay mutually distinct per theme, or the
    # stacked bar collapses into one block.
    for theme, colors in pal.items():
        ramp = [colors["op"], colors["uc"], colors["ann"]]
        assert len(set(c.upper() for c in ramp)) == 3, (
            f"theme {theme} status ramp is not 3 distinct colours: {ramp}")


def test_no_green_or_magenta_anywhere_in_the_renderer():
    src, _ = _module()
    for banned in BANNED_SUBSTRINGS:
        assert banned.lower() not in src.lower(), (
            f"{banned} is from the retired green/magenta palette — "
            f"the daily images are brand blue/black/violet now")


def test_chart_plate_is_never_left_at_matplotlib_default_white():
    """`_bars` must set the axes facecolor on EVERY call, not just when the
    theme happens to define chart_bg. The old form was:

        if "chart_bg" in pal:
            ax.set_facecolor(pal["chart_bg"])

    which left themes a/b/c on matplotlib's default white."""
    src, tree = _module()
    bars = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "_bars"), None)
    assert bars is not None, "_bars() not found — did the renderer get rewritten?"

    calls = [n for n in ast.walk(bars)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "set_facecolor"]
    assert calls, "_bars() no longer sets the axes facecolor at all"

    # None of those calls may sit inside an `if` — that is the bug.
    guarded = {id(c) for stmt in ast.walk(bars) if isinstance(stmt, ast.If)
               for c in ast.walk(stmt)
               if isinstance(c, ast.Call)
               and isinstance(c.func, ast.Attribute)
               and c.func.attr == "set_facecolor"}
    assert not guarded, (
        "ax.set_facecolor() is conditionally gated in _bars() — themes that "
        "miss the condition fall back to matplotlib's default WHITE plate")


def test_summary_labels_are_reachable_when_inline_numbers_is_off():
    """Theme C passes inline_numbers=False, summary_labels=True. The label
    loop used to be gated on inline_numbers alone with the summary branch
    nested inside it, so theme C shipped bar charts with no numbers on them."""
    src, tree = _module()
    bars = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_bars")
    tests = [ast.unparse(n.test) for n in ast.walk(bars) if isinstance(n, ast.If)]
    assert any("inline_numbers" in t and "summary_labels" in t for t in tests), (
        "the number-drawing loop must be entered when EITHER inline_numbers "
        "or summary_labels is set; found guards: " + repr(tests))

    # And theme C must still be asking for summary labels.
    assert re.search(r"inline_numbers=False,\s*summary_labels=True", src), (
        "theme C no longer requests summary labels")
