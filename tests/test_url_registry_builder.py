"""Guard routes.url_registry.build_public_url — the single chokepoint every
public dchub.cloud URL must flow through (kills the linkedin_404 bug class).

url_registry.py imports Flask at module load, and the CI unit-tests job
installs only pytest, so we AST-extract just slugify + _KIND_PATH + _BASE +
build_public_url and exec them in an isolated namespace seeded with re — same
pattern as test_marketing_topic_picker.py. Pure-function test, no Flask import.
"""
import os
import re
import ast

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UR = os.path.join(ROOT, "routes", "url_registry.py")

_WANT = {"slugify", "build_public_url", "_KIND_PATH", "_BASE", "_VALID_KINDS",
         # build_public_url calls this; the AST harness only execs names it
         # is told about, so a new free name here is a NameError at call
         # time, not an import error.
         "_strip_doubled_namespace"}


def _load_builder():
    src = open(UR, encoding="utf-8").read()
    tree = ast.parse(src)
    pieces = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _WANT:
            pieces.append(ast.get_source_segment(src, node))
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in _WANT for t in node.targets):
            pieces.append(ast.get_source_segment(src, node))
    ns = {"re": re}
    exec(compile("\n\n".join(pieces), UR, "exec"), ns)
    return ns["build_public_url"]


build_public_url = _load_builder()


def test_basic_kinds():
    assert build_public_url("dcpi", "santa-clara") == "https://dchub.cloud/dcpi/santa-clara"
    assert build_public_url("news", "big-news") == "https://dchub.cloud/news/big-news"
    assert build_public_url("markets", "phoenix") == "https://dchub.cloud/markets/phoenix"
    assert build_public_url("facility", "acme-dc") == "https://dchub.cloud/facility/acme-dc"


def test_press_release_uses_hyphenated_path():
    assert build_public_url("press_release", "kkr-deal") == "https://dchub.cloud/press-release/kkr-deal"


def test_slugify_is_idempotent_on_clean_slugs():
    # The whole migration relies on this: a column already holding a clean
    # market slug must round-trip unchanged through the builder.
    for s in ("santa-clara", "northern-virginia", "dallas-fort-worth"):
        assert build_public_url("dcpi", s) == f"https://dchub.cloud/dcpi/{s}"


def test_slugify_handles_non_string_input():
    # facility ids can be ints — must not crash (str() hardening).
    assert build_public_url("facility", 12345) == "https://dchub.cloud/facility/12345"
    # None falls back to 'update', never crashes or emits .../None
    assert build_public_url("dcpi", None) == "https://dchub.cloud/dcpi/update"


def test_subpath_appended_not_slugified():
    assert build_public_url("markets", "phoenix", subpath="brief") == \
        "https://dchub.cloud/markets/phoenix/brief"
    assert build_public_url("markets", "phoenix", subpath="brief.pdf") == \
        "https://dchub.cloud/markets/phoenix/brief.pdf"
    assert build_public_url("markets", "phoenix", subpath="/deep-dive/") == \
        "https://dchub.cloud/markets/phoenix/deep-dive"
    assert build_public_url("markets", "phoenix", subpath="brief/embed") == \
        "https://dchub.cloud/markets/phoenix/brief/embed"


def test_query_string_and_dict():
    assert build_public_url("markets", "phoenix", subpath="brief", query="embed=1") == \
        "https://dchub.cloud/markets/phoenix/brief?embed=1"
    assert build_public_url("markets", "phoenix", subpath="brief", query="?utm_source=embed") == \
        "https://dchub.cloud/markets/phoenix/brief?utm_source=embed"
    assert build_public_url("markets", "phoenix", subpath="brief", query={"embed": 1}) == \
        "https://dchub.cloud/markets/phoenix/brief?embed=1"


def test_invalid_kind_raises():
    import pytest
    with pytest.raises(ValueError):
        build_public_url("bogus", "x")


# --- slug rewriting: anchored to the kind namespace, nothing else ----------
# The builder's ONLY slug rewrite. It was written (d0d73821e) for a doubled
# `partnership-` PREFIX, then generalised in 0adf39da0 to collapse any adjacent
# identical parts anywhere in the slug. Both directions are pinned here because
# the generalised form silently rewrote real published URLs.


def test_doubled_namespace_prefix_still_collapses():
    """The case the dedupe exists for: a slug root that arrives already
    namespaced and then gets namespaced again."""
    assert build_public_url("news", "partnership-partnership-kkr-2026-w23") == \
        "https://dchub.cloud/news/partnership-kkr-2026-w23"
    assert build_public_url("press_release", "partnership-partnership-acme-2026-w01") == \
        "https://dchub.cloud/press-release/partnership-acme-2026-w01"
    # the kind's own path segment counts as its namespace too
    assert build_public_url("partners", "partners-partners-acme") == \
        "https://dchub.cloud/partners/partners-acme"
    assert build_public_url("press_release", "press-release-press-release-acme") == \
        "https://dchub.cloud/press-release/press-release-acme"


def test_a_repeated_word_inside_a_real_slug_is_not_collapsed():
    """Walla Walla (WA) and Baden-Baden are real places. The generalised
    collapse turned /markets/walla-walla into /markets/walla."""
    assert build_public_url("markets", "walla-walla") == \
        "https://dchub.cloud/markets/walla-walla"
    assert build_public_url("markets", "baden-baden") == \
        "https://dchub.cloud/markets/baden-baden"
    # control: a slug with no repeat is untouched either way
    assert build_public_url("markets", "ashburn") == \
        "https://dchub.cloud/markets/ashburn"


def test_dated_slug_whose_month_equals_its_day_survives():
    """The live blast radius: `2026-06-06` collapsed to `2026-06`, so a dated
    slug 404'd on 12 dates a year. Measured on two published releases."""
    for d in ("2026-01-01", "2026-06-06", "2026-07-07", "2026-12-12"):
        s = "dcpi-shift-" + d
        assert build_public_url("news", s) == "https://dchub.cloud/news/" + s
    # the two published (HTTP 200) press releases the old builder rebuilt as 404s
    assert build_public_url(
        "press_release", "2026-06-06-chatgpt-cites-dchub-top-intelligence-stack") == \
        "https://dchub.cloud/press-release/2026-06-06-chatgpt-cites-dchub-top-intelligence-stack"
    assert build_public_url(
        "press_release", "rural-spp-67-dcpi-build-headroom-2026-07-07") == \
        "https://dchub.cloud/press-release/rural-spp-67-dcpi-build-headroom-2026-07-07"


def test_a_doubled_first_token_that_is_not_a_namespace_survives():
    """3,329 of the published facility slugs start with a doubled OPERATOR
    token (`equinix-equinix-`, `2degrees-2degrees-`). Anchoring the strip to
    the start of the slug is not enough on its own — it must also be anchored
    to the kind's namespace, or all 3,329 get rewritten."""
    for s in ("equinix-equinix-dc11-ab12cd34",
              "2degrees-2degrees-auckland-albany-901b53d2",
              "aapt-aapt-braddon-cef530f4"):
        assert build_public_url("facility", s) == "https://dchub.cloud/facility/" + s


def test_the_namespace_strip_is_scoped_to_the_kind_being_built():
    """`reports-reports-` is a doubled namespace for kind=reports and real slug
    content for kind=facility — and the published facility slug
    reports-reports-contracted-revenue-f7429199 proves the second case is live."""
    assert build_public_url("reports", "reports-reports-q3-capacity") == \
        "https://dchub.cloud/reports/reports-q3-capacity"
    assert build_public_url("facility", "reports-reports-contracted-revenue-f7429199") == \
        "https://dchub.cloud/facility/reports-reports-contracted-revenue-f7429199"


def test_the_strip_takes_one_level_and_only_at_the_start():
    # not a prefix -> untouched
    assert build_public_url("news", "kkr-partnership-partnership-deal") == \
        "https://dchub.cloud/news/kkr-partnership-partnership-deal"
    # one level per call, not a loop to fixpoint
    assert build_public_url("news", "partnership-partnership-partnership-x") == \
        "https://dchub.cloud/news/partnership-partnership-x"
