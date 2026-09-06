"""tests/test_ai_learn_capabilities_derived.py — /ai/learn publishes no hand-typed number (2026-09-06).

r-news-sources. /ai/learn's `capabilities` dict is four headline numbers in one
object, and until this change three of the four were typed by hand:

    {'tools': 51, 'facilities': _canon_text('{canon_facilities}'),
     'countries': 178, 'sources': 40}

Every hand-typed one was wrong. `tools: 51` was on ai_surface_canon's OWN
stale_markers denylist against a canon 83. `countries: 178` was an EXACT count
where the canon publishes a FLOOR. `sources: 40` was the interesting one: it had
no canonical owner at all — no pin, no derivation, no endpoint publishing it —
so unlike its siblings it could not be checked in EITHER direction, and it was
low by ~61x against a measured COUNT(DISTINCT source) FROM announcements = 2,442.

★ WHAT THIS GUARD IS FOR, AND WHY IT IS NOT A VALUE ASSERTION. The numbers move;
pinning them here would just relocate the drift into the test suite. What must
never come back is the SHAPE — a bare literal in the capabilities block. So this
walks main.ai_learn()'s AST and fails on any numeric constant inside it.

★ AND IT FENCES THE LABEL, WHICH THE NUMBER CANNOT. `sources` was unscoped: a
bare key between `facilities` and `countries` reads as "40 DATA sources" (DC Hub
publishes ~330,000 mapped assets from dozens of feeds) rather than the "40+ NEWS
sources" every sibling surface meant. One number, two meanings, one page. The
key is `news_sources` now and this file fails if it reverts.

Run:  python3 -m pytest tests/test_ai_learn_capabilities_derived.py -v
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _ai_learn_node() -> ast.FunctionDef:
    """The AST of main.ai_learn(). Parsed, not imported — main.py is 45k lines
    and importing it pulls the whole app up for a four-key dict."""
    tree = ast.parse((_ROOT / "main.py").read_text(encoding="utf-8", errors="replace"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "ai_learn":
            return node
    pytest.fail("main.ai_learn() not found — this guard would pass vacuously")


def _capabilities_dict(fn: ast.FunctionDef) -> ast.Dict:
    """The `capabilities` value inside ai_learn's `topics` dict."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and k.value == "capabilities":
                assert isinstance(v, ast.Dict), "capabilities is no longer a dict literal"
                return v
    pytest.fail("ai_learn() has no 'capabilities' dict — guard would pass vacuously")


def test_capabilities_keys_are_scoped():
    """A headline key must say WHAT it counts.

    `sources` is banned outright: it is the name that let a news figure be read
    as a data-feed figure. `news_sources` says which."""
    caps = _capabilities_dict(_ai_learn_node())
    keys = [k.value for k in caps.keys if isinstance(k, ast.Constant)]
    assert keys, "capabilities dict has no literal keys — guard would pass vacuously"
    assert "sources" not in keys, (
        "capabilities['sources'] is back. An unlabelled `sources` between "
        "`facilities` and `countries` reads as '<n> DATA sources', not "
        "'<n> NEWS sources' — the mislabel half of r-news-sources. Use "
        "'news_sources' (and {canon_news_sources}, which is named to match)."
    )
    assert "news_sources" in keys, f"expected a news_sources key, got {keys}"


def test_no_capability_figure_is_hand_typed():
    """THE PIN: no numeric literal may sit in the capabilities block.

    Not 'the numbers are right' — they move. The defect is the SHAPE: a literal
    here cannot be swept, cannot be sentinel-checked and cannot be healed, which
    is exactly how `sources: 40` survived on ~47 files without ever touching a
    measurement."""
    caps = _capabilities_dict(_ai_learn_node())
    literals = []
    for k, v in zip(caps.keys, caps.values):
        name = k.value if isinstance(k, ast.Constant) else "<computed>"
        for sub in ast.walk(v):
            # A bare number. Strings are fine — a canon FLOOR phrase ("170+",
            # "2,000+") is a string by nature, and _canon_int's fallback arg is
            # read from the canon rather than typed, so it is a Name not a Num.
            if isinstance(sub, ast.Constant) and isinstance(sub.value, (int, float)) \
                    and not isinstance(sub.value, bool):
                literals.append(f"  capabilities[{name!r}] contains the literal {sub.value!r}")
    assert not literals, (
        "/ai/learn publishes a hand-typed headline number. Every figure in this "
        "block must resolve through ai_surface_canon — _canon_text() for floor "
        "phrases, _canon_int() for exact counts:\n" + "\n".join(literals)
    )


def test_every_capability_value_reaches_a_resolver():
    """Each value must be a resolver CALL, not a constant of any type.

    A string constant would satisfy the numeric guard above while re-freezing
    the value as copy — the '20,100+ facilities' failure mode one type over."""
    caps = _capabilities_dict(_ai_learn_node())
    resolvers = {"canon_text", "_canon_text", "_canon_int"}
    bad = []
    for k, v in zip(caps.keys, caps.values):
        name = k.value if isinstance(k, ast.Constant) else "<computed>"
        calls = [n.func.id for n in ast.walk(v)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        if not (set(calls) & resolvers):
            bad.append(f"  capabilities[{name!r}] reaches no resolver (calls: {calls or 'none'})")
    assert not bad, (
        "A capabilities value does not resolve through the canon:\n" + "\n".join(bad))


def test_news_sources_floor_never_exceeds_the_measurement():
    """Floors round DOWN. The invariant every re-flooring in _FALLBACK restored.

    Runs against the SEED when no DB is reachable, which is the case this most
    needs to hold for: the seed IS what a cold start publishes."""
    from canonical_stats import _FALLBACK, news_sources_phrase
    seed = int(_FALLBACK["news_sources"])
    floored = int(re.sub(r"[^\d]", "", news_sources_phrase()))
    assert floored <= seed, (
        f"news_sources floor {floored} exceeds its own cold-start seed {seed} — "
        "a floor above reality is the defect that re-floored facilities_verified "
        "three times in June 2026.")


def test_retired_source_markers_cannot_match_the_published_floor():
    """The 09-02 lesson: a canon must not denylist its own answer.

    ai_surface_sentinel scans served bodies with PLAIN SUBSTRING matching, so
    "40+ sources" would also match "1,240+ sources". news_sources_phrase()
    floors with step=1000 so the published value always ends ',000+' — this
    asserts that property rather than trusting the comment that claims it."""
    from ai_surface_canon import PINNED, canon_nums
    from canonical_stats import _PUBLIC_FLOOR_SPECS

    markers = [m for m in (PINNED.get("stale_markers") or []) if "source" in m]
    assert markers, "the retired source markers are gone — guard would pass vacuously"

    floor = _PUBLIC_FLOOR_SPECS["news_sources"][1]
    published = {canon_nums()["{canon_news_sources}"]}
    # every value the floor can emit across the plausible live range
    published |= {floor(n) for n in range(1, 40_001, 137)}
    collisions = [(m, p) for m in markers for p in published
                  if p and m in f"Industry news aggregated from {p} sources"]
    assert not collisions, (
        "A retired-source marker matches a value the canon itself publishes. "
        "ai_surface_sentinel would flag every correct surface as drifted — the "
        "exact failure that retired the five '2,000+ deals' markers on "
        f"2026-09-02:\n" + "\n".join(f"  marker {m!r} matches published {p!r}"
                                     for m, p in collisions))
