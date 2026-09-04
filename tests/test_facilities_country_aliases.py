"""A guessed country NAME must reach the country hub, not a 404.

★ THE DEFECT. /facilities/in/<code> is canonical and the route 404s every other
form. Measured 2026-09-04 through the edge as Googlebot:

    /facilities/in/germany        404      /facilities/in/de   200
    /facilities/in/france         404      /facilities/in/fr   200
    /facilities/in/japan          404      /facilities/in/jp   200
    /facilities/in/united-states  404      /facilities/in/us   200
    /facilities/in/netherlands    404      /facilities/in/nl   200
    /facilities/in/singapore      404      /facilities/in/sg   200

The human-readable name is exactly what an assistant emits when it cites a
country hub, so the citation lands on a 404. Note the US-STATE route
canonicalises the opposite way -- the 2-letter code 301s to the name slug --
so a guessed name works for states and fails for countries. That inconsistency
is what makes the 404 easy to ship and hard to notice.

★ WHY AN ALLOWLIST AND NOT A WILDCARD. The 404 in that branch is load-bearing:
its own comment records that all 676 two-letter codes once answered 200 with
near-identical empty shells. These tests therefore assert BOTH directions --
the 46 real names redirect AND a junk code still 404s. Widening the alias map
to a catch-all would pass the first half and silently reopen the hole.
"""
import logging

import pytest
from flask import Flask

logging.disable(logging.CRITICAL)
from facilities_hub import _COUNTRY_BY_SLUG, _COUNTRY_NAMES, facilities_hub_bp


@pytest.fixture(scope="module")
def client():
    app = Flask(__name__)
    app.register_blueprint(facilities_hub_bp)
    return app.test_client()


@pytest.mark.parametrize(
    "slug,code",
    [("germany", "de"), ("france", "fr"), ("japan", "jp"),
     ("united-states", "us"), ("netherlands", "nl"), ("singapore", "sg")],
)
def test_measured_404s_now_redirect(client, slug, code):
    """The six paths measured 404 above. Regression anchors, not examples."""
    r = client.get(f"/facilities/in/{slug}")
    assert r.status_code == 301, f"/facilities/in/{slug} returned {r.status_code}"
    assert r.headers["Location"].endswith(f"/facilities/in/{code}")


def test_every_displayed_country_name_is_reachable(client):
    """The invariant: any country whose name we DISPLAY, we accept as a URL.

    Asserted over the whole map rather than a sample, so adding a country to
    _COUNTRY_NAMES without a working alias fails here instead of in the wild.
    """
    for code, name in _COUNTRY_NAMES.items():
        slug = next(s for s, c in _COUNTRY_BY_SLUG.items() if c == code.lower())
        r = client.get(f"/facilities/in/{slug}")
        assert r.status_code == 301, f"{name} ({slug}) -> {r.status_code}, expected 301"
        assert r.headers["Location"].endswith(f"/facilities/in/{code.lower()}")


def test_pagination_survives_the_redirect(client):
    r = client.get("/facilities/in/germany/page/3")
    assert r.status_code == 301
    assert r.headers["Location"].endswith("/facilities/in/de/page/3")


# ★ THESE TWO REPLACED A VACUOUS TEST. The first version parametrised junk
#   2-LETTER codes ("qz", "ww", "oa", "zz") and asserted they do not redirect.
#   It passed against a deliberately catch-all alias map -- 13/13 green -- so it
#   was guarding nothing: a 2-letter junk code resolves to ITSELF, and the
#   `alias != country` condition in the route already suppresses that case by
#   construction. The assertion could not fail. Junk must therefore be a slug
#   that is NOT a country code, which is what a widened map would actually
#   start redirecting.
@pytest.mark.parametrize(
    "junk",
    ["atlantis", "made-up-country", "north-dakota", "qz", "ww", "europe", "asia"],
)
def test_alias_map_holds_only_real_country_names(junk):
    """Map-level, so it runs without the DB and fails on a catch-all.

    A widened map returns SOMETHING here; the finite inverted map returns None.
    """
    assert _COUNTRY_BY_SLUG.get(junk) is None, (
        f"{junk!r} resolved to {_COUNTRY_BY_SLUG.get(junk)!r} -- the alias map "
        "is no longer a finite allowlist and the 676-shell space is reopening"
    )


def test_a_non_country_slug_does_not_redirect(client):
    """The same property at HTTP level, on a slug a catch-all WOULD redirect."""
    r = client.get("/facilities/in/atlantis")
    assert r.status_code != 301, (
        f"/facilities/in/atlantis redirected to "
        f"{r.headers.get('Location')!r} -- alias map went wild"
    )


def test_canonical_code_is_not_itself_redirected(client):
    """'de' must serve, not bounce -- a self-redirect here is an infinite loop."""
    assert _COUNTRY_BY_SLUG.get("de") in (None, "de")
