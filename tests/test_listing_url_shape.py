#!/usr/bin/env python3
"""tests/test_listing_url_shape.py

ONE LISTING, TWO URL SHAPES, AND THE REASON BOTH SURVIVE.

The Capacity Source listing URL was emitted in two disagreeing shapes. Measured
against the live edge 2026-09-15 (cache-busted, `cf-cache-status: MISS`):

  /listings/<slug>    200, `x-robots-tag: index, follow`, SELF-CANONICAL
                      (<link rel=canonical> is the same URL), per-listing
                      <title>, served by the worker
                      (x-dc-worker-version: 5.0.0-listing-slug-teaser-pages-
                      2026-09-15). An unknown slug is a real 404 + noindex.
                      BUT: it is a 6 KB STATIC teaser -- no app bundle, no
                      #root, ~955 characters of text -- and its own primary CTA
                      links back to /listings?l=<slug>. It never hydrates.

  /listings?l=<slug>  200, but <link rel=canonical> points at bare
                      https://dchub.cloud/listings and the <title> is the
                      generic index title. It is the SPA, and it is the only
                      form that can show a signed-in caller their listing.

So the two shapes are not a bug to collapse -- they are two different jobs:

  PUBLIC / CANONICAL  `_listing_path` -> /listings/<slug>. The `url` on every
                      teaser card, the buyer "Open the listing" email link, the
                      persona-brief listing link, the sitemap row.
  SIGN-IN RETURN      `_return_path` -> /listings?l=<slug>. Everything handed to
                      `_sign_in_url` / `_access`, because sending a caller who
                      just authenticated to a static teaser strands them.

Publishing the ?l= form was not cosmetic: a sitemap row whose page
canonicalises elsewhere is a guaranteed "Alternate page with proper canonical"
in Search Console -- the exact defect
tests/test_sitemap_publishes_only_served_selfcanonical_urls.py exists to stop.

★ EVERY TEST HERE FAILS ON THE PRE-FIX TREE, and the two AST scans carry floors
so that renaming the thing they read cannot turn them vacuously green.
"""
import ast
import io
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import routes.exclusive_listings as el  # noqa: E402

SITE = "https://dchub.cloud"


# ── the split, measured on the shipped helpers ────────────────────────────

def test_the_public_listing_url_is_the_per_listing_page():
    assert el._listing_path("dfw-40") == "/listings/dfw-40"
    assert el._listing_url("dfw-40") == f"{SITE}/listings/dfw-40"


def test_the_sign_in_return_path_stays_on_the_index_form():
    """NOT swept along with the canonical move. /listings/<slug> is a static
    teaser that never hydrates, so a caller returning there after sign-in sees
    ~955 characters of public teaser instead of the listing they unlocked."""
    assert el._return_path("dfw-40") == "/listings?l=dfw-40"


def test_the_two_helpers_are_actually_two():
    """The whole point of the change. If someone later collapses them, this is
    the test that says which one they broke."""
    assert el._listing_path("dfw-40") != el._return_path("dfw-40")


def test_sign_in_still_round_trips_to_the_form_the_app_can_read():
    url = el._sign_in_url(el._return_path("dfw-40"))
    assert url == f"{SITE}/login?redirect=%2Flistings%3Fl%3Ddfw-40"


# ── shape details a careless rewrite gets wrong ───────────────────────────

def test_a_slug_is_escaped_as_one_path_segment():
    """safe='' -- a slug carrying a slash must not silently become two path
    segments and point at a different page."""
    assert el._listing_path("a b/c") == "/listings/a%20b%2Fc"


@pytest.mark.parametrize("slug", [None, "", 0])
def test_an_empty_slug_does_not_emit_a_trailing_slash_page(slug):
    """/listings/ and /listings are different URLs; only the second is real."""
    assert el._listing_path(slug) == "/listings"
    assert el._listing_url(slug) == f"{SITE}/listings"


# ── the teaser card every caller actually reads ───────────────────────────

def test_the_teaser_card_carries_the_canonical_url():
    row = {"id": 1, "slug": "dfw-40", "title": "Powered shell — DFW",
           "summary": "s", "status": "pocket", "market": "Dallas", "state": "TX",
           "country": "US", "capacity_mw": 40, "detail": None,
           "created_at": None, "updated_at": None, "expires_at": None}
    access = {"required": "registered", "granted": False,
              "reason": "sign_in_required", "unlock": None}
    assert el._teaser(row, access)["url"] == f"{SITE}/listings/dfw-40"


# ── the other two emitters, read from the AST so prose cannot satisfy it ──

def _listing_literals(path):
    """Every string literal in `path` that mentions /listings, rendered as a
    skeleton with `{}` where an f-string interpolates. Read from the AST, so a
    comment quoting the old shape can neither satisfy nor trip these."""
    tree = ast.parse(io.open(os.path.join(ROOT, path), encoding="utf-8").read())
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            s = "".join(v.value if isinstance(v, ast.Constant) else "{}"
                        for v in node.values)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            s = node.value
        else:
            continue
        if "/listings" in s:
            out.append(s)
    return out


@pytest.mark.parametrize("path,floor", [
    ("routes/persona_briefs.py", 1),
    ("routes/sitemap_auto.py", 1),
])
def test_no_module_still_publishes_the_index_form_as_a_listing_url(path, floor):
    lits = _listing_literals(path)
    # The floor. Without it a rename that makes _listing_literals find nothing
    # would pass this test while the module published anything it liked.
    assert len(lits) >= floor, (
        f"{path}: found no /listings string literals at all, so the assertion "
        f"below is vacuous — the scan, not the module, is what broke")
    per_listing = [s for s in lits if "/listings/{}" in s]
    assert per_listing, (
        f"{path} builds no per-listing /listings/<slug> URL; it still emits "
        f"only {lits}")
    index_form = [s for s in lits if "/listings?l=" in s]
    assert not index_form, (
        f"{path} still publishes the ?l= index form as a listing URL: "
        f"{index_form}. That page canonicalises to bare /listings.")
