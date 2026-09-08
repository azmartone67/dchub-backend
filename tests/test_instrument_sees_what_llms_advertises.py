"""If we advertise a URL, the instrument must be able to SEE a fetch of it.

★ THE GAP, measured 2026-09-08. llms.txt and llms-full.txt advertise 14 worked
examples carrying a query string. #4052 and #4090 exist entirely to let crawlers
fetch them. But ai_tracking.AI_ENDPOINT_PATTERNS matched only ELEVEN:

    BLIND  /api/ai/query?type=deals
    BLIND  /api/renewable/solar?lat=36.17&lon=-115.14
    BLIND  /api/energy/prices/TX

A crawler fetching those three was recorded by NOBODY — not by the Flask hook
(no pattern) and not by the edge beacon (ORGANIC_CONTENT_PREFIXES is content
pages only, and its UA regex has no bingbot).

★ WHY THAT MATTERED MORE THAN THREE MISSING ROWS. It made the post-#4090
question unanswerable in the direction that mattered. copilot — the Bingbot
bucket — sat flat at ~12/day of `ambiguous_data_api` across the deploy while
claude went 134 -> 7,437 the same day. "Bingbot did not move" was a claim about
11 of the 14 URLs, and nothing told the reader which 11. A partial instrument
does not produce a smaller answer; it produces an answer of unknown shape.

★ DERIVED, so it cannot drift again. The requirement is computed FROM the
advertised URLs — the same source tests/test_robots_permits_what_llms_advertises
derives its robots contract from. Advertise a new endpoint and this guard starts
demanding the instrument can see it, with no edit here.

★ ONE MORE THING THIS DOES NOT CLAIM. Coverage is not attribution: seeing a
fetch tells you it happened, not which crawler benefited from which robots line.
And the step in ai_requests when this ships is INSTRUMENTATION, not demand — the
same shape as the 2026-09-05 beacon switch-on. Do not compare across that date
without saying so.
"""
import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIBLING = os.path.join(ROOT, "tests", "test_robots_permits_what_llms_advertises.py")


def _advertised_urls():
    """The advertised set, taken from the guard that already derives it.

    IMPORTED rather than re-extracted. util/deals.py's note is the house rule —
    "IMPORT the predicate, never re-inline" — and two copies of an llms-template
    slicer would drift the day either template moves. Loaded by PATH so this does
    not depend on `tests` being an importable package.
    """
    if not os.path.exists(SIBLING):
        pytest.skip(f"{SIBLING} is gone — re-home this derivation")
    spec = importlib.util.spec_from_file_location("_adv_sibling", SIBLING)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return list(getattr(mod, "ADVERTISED", []) or []), list(getattr(mod, "WITH_QUERY", []) or [])


def _path_of(url: str) -> str:
    tail = url.split("dchub.cloud", 1)[-1] if "dchub.cloud" in url else url
    return tail.split("?", 1)[0].split("#", 1)[0] or "/"


def test_the_derived_sets_are_not_empty():
    """Floor. The assertion below loops over the advertised set; on an empty one
    it passes while proving nothing — the exact shape the sibling's own Floors
    section exists to prevent."""
    adv, qs = _advertised_urls()
    assert len(adv) >= 40, (
        f"only {len(adv)} advertised URLs parsed — the sibling's slice anchors "
        "probably moved, and the real assertion below would pass vacuously.")
    assert len(qs) >= 10, (
        f"only {len(qs)} advertised URLs carry a query string; those are the "
        "class #4052/#4090 unblocked and the class this instrument must see.")


def test_the_pattern_list_is_not_trivial():
    """Second floor, other side: a shrunken pattern list would make the assertion
    below fail loudly, but an EMPTY one would make is_ai_endpoint match nothing
    and fail in a way that reads like a coverage bug rather than a parse bug."""
    import ai_tracking
    pats = list(getattr(ai_tracking, "AI_ENDPOINT_PATTERNS", []) or [])
    assert len(pats) >= 10, f"only {len(pats)} endpoint patterns — list shape changed"


def test_every_advertised_url_is_visible_to_the_instrument():
    import ai_tracking
    adv, _ = _advertised_urls()
    blind = sorted({_path_of(u) for u in adv
                    if _path_of(u).startswith("/api/")
                    and not ai_tracking.is_ai_endpoint(_path_of(u))})
    assert not blind, (
        "advertised /api URL path(s) that is_ai_endpoint() cannot see: "
        f"{blind}. A crawler fetching these is recorded by nobody — the Flask "
        "hook skips them and the edge beacon only covers content pages. Add a "
        "pattern to ai_tracking.AI_ENDPOINT_PATTERNS; do NOT narrow this guard.")


def test_the_three_paths_that_were_blind_are_covered():
    """Named explicitly as well as derived. The derived assertion above is the
    contract, but naming the actual regression means a future edit that drops one
    of these fails with the history attached rather than as an anonymous diff."""
    import ai_tracking
    for path in ("/api/ai/query", "/api/renewable/solar", "/api/energy/prices/TX"):
        assert ai_tracking.is_ai_endpoint(path), (
            f"{path} is invisible to the instrument again — it was one of the "
            "three that made the post-#4090 Bingbot reading a claim about 11 of "
            "14 URLs.")
