"""The last two homes of the stale floor: operator pages and the site report.

MEASURED 2026-09-17, against /api/v1/canon/phrases:

  routes/operators.py   x2   "Get all 19,000+ facilities ... from $49/mo"
  routes/site_report.py      stats tiles:
        19,000+ facilities   canon 21,900+   understated
        233 markets          canon 300+      understated
        170+ countries       canon 170+      correct
        4,000+ M&A deals     canon 2,200+    ~2x OVER-CLAIM

★ THE DEALS FIGURE IS NOT DRIFT. It is the exact over-claim ai_surface_canon
retired on 2026-07-17, recorded there in its own words: "was '4,000+', itself
an over-claim — it floored ROWS, and the AUTO id embeds the ingest date so one
deal accrues a row per day (4,275 rows -> ~1,420 distinct)". It stayed in a
client-facing report — one whose own intro says "Every figure in this report is
sourced, attributed, and refreshable" — for two months after canon dropped it.

WHAT THESE PIN
  * no count and no price is a literal on either surface;
  * a figure the canon cannot resolve is DROPPED, never rendered blank and
    never left as a stale literal — canon_text is fail-open, so an empty tile
    is the failure mode to design against;
  * an UNRESOLVED placeholder ("{canon_deals}") never reaches a page;
  * the two operator surfaces do not render identical blocks;
  * facility pages stay out of scope.
"""
import ast
import pathlib
import re
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OPS = (ROOT / "routes" / "operators.py").read_text(encoding="utf-8")
RPT = (ROOT / "routes" / "site_report.py").read_text(encoding="utf-8")

from routes.operators import _operator_offer_html as offer  # noqa: E402
from routes.site_report import _canon_stat_tiles as tiles  # noqa: E402


def text(html):
    t = re.sub(r"<[^>]+>", " ", html or "")
    t = t.replace("&mdash;", "—").replace("&middot;", "·").replace("&#39;", "'")
    return re.sub(r"\s+", " ", t).strip()


def _code_of(src, name):
    """Executable body only — the comments here quote 19,000+ and $49 on
    purpose, and a guard that reads prose as code is the defect one level up."""
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.FunctionDef) and n.name == name)
    nodes = fn.body
    if (nodes and isinstance(nodes[0], ast.Expr)
            and isinstance(nodes[0].value, ast.Constant)
            and isinstance(nodes[0].value.value, str)):
        nodes = nodes[1:]
    code = "\n".join(ast.get_source_segment(src, n) or "" for n in nodes)
    assert code.strip(), f"{name}: extracted an empty body"
    return code


# ── the literals are gone ───────────────────────────────────────────────
@pytest.mark.parametrize("src,name", [("OPS", "operators.py"),
                                      ("RPT", "site_report.py")])
def test_no_stale_floor_is_rendered(src, name):
    s = {"OPS": OPS, "RPT": RPT}[src]
    # only the comment that records what was replaced may still say it
    for literal in ("19,000+", "from $49/mo", "4,000+", '"233"'):
        for m in re.finditer(re.escape(literal), s):
            line = s[s.rindex("\n", 0, m.start()) + 1:
                     s.index("\n", m.end())].lstrip()
            assert line.startswith("#"), (
                f"{name}: live {literal!r} outside a comment: {line[:80]}")


def test_neither_builder_types_a_count_or_a_price():
    for src, fn in ((OPS, "_operator_offer_html"), (RPT, "_canon_stat_tiles")):
        code = _code_of(src, fn)
        for bad in ("19,000", "21,900", "$49", "$99", "$10", "2,200", "233",
                    "4,000"):
            assert bad not in code, f"{fn}: {bad!r} is typed into the builder"


# ── resolution, and what happens when it fails ──────────────────────────
def test_the_operator_block_reads_the_canon_and_both_registries():
    from routes.mcp_conversion_plays import PACK10_CREDITS, PACK10_PRICE_CENTS
    import tier_registry as tr
    from ai_surface_canon import canon_text
    t = text(offer("Tracking X? DC Hub gives you"))
    assert (canon_text("{canon_facilities}") or "").strip() in t
    assert f"${PACK10_PRICE_CENTS // 100} one-time" in t
    assert f"{PACK10_CREDITS:,} API calls" in t
    assert f"Pro ${int(tr.price('pro'))}/mo" in t


def test_an_unresolvable_count_is_dropped_not_guessed(monkeypatch):
    """★ canon_text is FAIL-OPEN — it yields '' when the canon cannot be read.
    The old literal is exactly what a naive fallback would restore."""
    monkeypatch.setitem(sys.modules, "ai_surface_canon", None)
    t = text(offer("Tracking X? DC Hub gives you"))
    assert "19,000" not in t and "21,900" not in t
    assert "the full facility, power and site-selection layer" in t
    assert "Pro $" in t, "the prices went with the count"


def test_an_unresolvable_price_drops_its_rung(monkeypatch):
    monkeypatch.setitem(sys.modules, "routes.mcp_conversion_plays", None)
    t = text(offer("Tracking X? DC Hub gives you"))
    assert "one-time" not in t
    assert "Pro $" in t


def test_with_nothing_readable_it_still_renders_and_names_no_number(monkeypatch):
    for mod in ("ai_surface_canon", "routes.mcp_conversion_plays", "tier_registry"):
        monkeypatch.setitem(sys.modules, mod, None)
    t = text(offer("Tracking X? DC Hub gives you"))
    assert "dchub.cloud/pricing" in t
    assert "$" not in t, "a price was guessed with no registry readable"


# ── the report tiles ────────────────────────────────────────────────────
def test_every_tile_resolves_from_the_canon():
    from ai_surface_canon import canon_text
    got = {d["l"]: d["v"] for d in tiles()}
    for label, placeholder in (("facilities tracked", "{canon_facilities}"),
                               ("markets", "{canon_markets}"),
                               ("countries", "{canon_countries}"),
                               ("M&A deals", "{canon_deals}")):
        want = (canon_text(placeholder) or "").strip()
        if want:
            assert got.get(label) == want, label


def test_the_deals_tile_is_no_longer_the_retired_over_claim():
    """★ 4,000+ floored ROWS, not distinct deals — canon retired it 2026-07-17
    and it survived here for two months in a client deliverable."""
    got = {d["l"]: d["v"] for d in tiles()}
    assert got.get("M&A deals") != "4,000+"
    assert got.get("markets") != "233"


def test_an_unresolvable_tile_is_dropped_not_blank(monkeypatch):
    """★ A blank value would publish '  facilities tracked' on a sourced
    report. Fewer tiles is visible and harmless; a blank number is not."""
    import ai_surface_canon as c
    monkeypatch.setattr(c, "canon_text", lambda p: "")
    assert tiles() == []


def test_an_unresolved_placeholder_never_ships(monkeypatch):
    """The failure ai_surface_canon's own docstring calls the one to fear:
    serving the literal '{canon_deals}' to a reader."""
    import ai_surface_canon as c
    monkeypatch.setattr(c, "canon_text", lambda p: p)   # returns it unresolved
    assert tiles() == []


def test_a_canon_import_failure_yields_no_tiles(monkeypatch):
    monkeypatch.setitem(sys.modules, "ai_surface_canon", None)
    assert tiles() == []


# ── scope ───────────────────────────────────────────────────────────────
def test_the_two_operator_surfaces_do_not_render_the_same_block():
    hub = text(offer("Tracking data-center operators? DC Hub gives you"))
    one = text(offer("Tracking Equinix's portfolio? DC Hub gives you"))
    assert hub != one
    assert "Equinix" in one and "Equinix" not in hub


def test_the_per_operator_call_site_passes_the_operator_name():
    """★ ANCHORED TO THE CALL SITE, not to the file. The first version asserted
    `summary['name']` appeared somewhere in operators.py — it does, in the card
    rendering — so pointing the per-operator block at the HUB's generic lead
    passed the guard untouched. Caught by mutation; the assertion was the weak
    part, not the code."""
    calls = [ln for ln in OPS.splitlines() if "_operator_offer_html(" in ln
             and "def " not in ln]
    assert len(calls) == 2, f"expected 2 call sites, found {len(calls)}"
    named = [c for c in calls if "summary['name']" in c]
    assert len(named) == 1, (
        "the per-operator block no longer passes the operator's name — both "
        f"surfaces would render identical boilerplate: {calls}")
    assert len(set(calls)) == 2, "both call sites pass the SAME lead"


def test_facility_pages_stay_out_of_scope():
    """Identical boilerplate across 21,900+ facility pages is what
    thin_content_master_shell exists to catch."""
    assert "_operator_offer_html" not in (
        (ROOT / "routes" / "seo_pages.py").read_text(encoding="utf-8")
        if (ROOT / "routes" / "seo_pages.py").exists() else "")
