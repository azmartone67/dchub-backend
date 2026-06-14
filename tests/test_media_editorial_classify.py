"""r86d: guard the media editorial desk's engagement-attribution classifier.

The bandit (engagement_by_kind) learns which ANGLES earn reach by classifying
each LinkedIn post back into the desk's lead KIND. If _classify_kind mislabels
the desk's OWN lead headlines (e.g. a mover post tagged dcpi_build because both
mention "DCPI excess-power"), the bandit learns from a corrupted label set and
silently optimizes the wrong thing. These tests assert every lead kind that
rank_data_events() emits round-trips to itself, so a future pattern edit can't
re-introduce the mis-attribution.

NOTE: the CI unit-tests job installs ONLY pytest (not requirements.txt), so a
plain `from routes.media_editorial import _classify_kind` crashes collection
(routes/media_editorial.py imports Flask). We AST-extract just the classifier
helper + its _KIND_PATTERNS table and exec them in an isolated namespace seeded
with `re` — same pattern as test_marketing_topic_picker.py — so the test runs
as a pure function with no Flask/DB import.
"""
import os
import re
import ast

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ME = os.path.join(ROOT, "routes", "media_editorial.py")


def _load_classify_kind():
    src = open(ME, encoding="utf-8").read()
    tree = ast.parse(src)
    pieces = []
    for node in tree.body:
        # _KIND_PATTERNS = [ ... re.compile(...) ... ]  (not a literal, so we
        # can't literal_eval it; grab it by name and exec the source verbatim)
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_KIND_PATTERNS"
                for t in node.targets):
            pieces.append(ast.get_source_segment(src, node))
        elif isinstance(node, ast.FunctionDef) and node.name == "_classify_kind":
            pieces.append(ast.get_source_segment(src, node))
    ns = {"re": re}
    exec(compile("\n\n".join(pieces), ME, "exec"), ns)
    fn = ns.get("_classify_kind")
    assert fn, "_classify_kind not found in media_editorial.py"
    return fn


_classify_kind = _load_classify_kind()


# One representative headline per kind, matching the exact templates built in
# media_editorial.rank_data_events().
CASES = {
    "deal":            "$10.0B data-center transaction: KKR/Nvidia",
    "dcpi_mover":      "Cheyenne climbed 12 pts on the DCPI excess-power index this week",
    "dcpi_build":      "Cheyenne leads the DCPI excess-power index at 70/100",
    "interconnection": "ERCOT's interconnection queue holds 427 GW of requested load, 38% of all US queued load",
    "new_facility":    "Aligned: 250 MW in TX just entered the tracker",
}


def test_each_desk_kind_round_trips():
    for expected_kind, headline in CASES.items():
        assert _classify_kind(headline) == expected_kind, (
            f"{headline!r} classified as {_classify_kind(headline)!r}, "
            f"expected {expected_kind!r}")


def test_deal_with_power_context_not_stolen_by_interconnection():
    # A deal post whose body references power-rich ISO context (mentions GW)
    # must still classify as a deal — the $-amount cue is checked first.
    txt = "$3.2B data-center acquisition in ERCOT, a 12 GW-queue market"
    assert _classify_kind(txt) == "deal"


def test_unknown_text_is_other():
    assert _classify_kind("DC Hub is the recognized authority on data centers") == "other"
    assert _classify_kind("") == "other"
