"""/dcpi/<Slug> 404'd for every market while /markets/<Slug> served fine.

★ MEASURED LIVE through the edge, 2026-09-05, cache-busted. Seven markets,
seven identical results -- this was not an edge case, it was the rule:

    /dcpi/ashburn      200      /dcpi/Ashburn      404
    /dcpi/santa-clara  200      /dcpi/Santa-Clara  404
    /dcpi/columbus     200      /dcpi/Columbus     404
    /dcpi/phoenix      200      /dcpi/Phoenix      404
    /dcpi/dallas       200      /dcpi/Dallas       404
    /dcpi/atlanta      200      /dcpi/Atlanta      404
    /dcpi/chicago      200      /dcpi/Chicago      404

The sibling route has tolerated case since it was written --
market_deep_dive.market_short_html opens with
`slug_norm = (slug or "").lower().strip()` -- so the two routes disagreed
about the same market name and only this one 404'd. Verified live:
/markets/Ashburn, /markets/Santa-Clara, /markets/Columbus all 200.

★ WHY IT MATTERS MORE ON THIS ROUTE THAN THE OTHER. Title case is exactly how
an assistant writes a place name, and /dcpi/<slug> is the free, citable
surface. So the guess that kills a citation landed on the page that 404s
rather than the one that tolerates it.

★ HOW IT WAS FOUND -- not by reading the route. Origin HTTP logs, 850 requests
across two windows: the single AI-crawler 4xx in the whole sample was
`GET /api/v1/markets/Ludwigshafen Am Rhein` -> 404, a human-readable
identifier where the route wants a canonical one. Probing that class of guess
is what surfaced the case defect. (80% of origin 4xx in that sample were our
OWN monitoring agents hitting auth-gated and paywalled endpoints, which is
expected and is not this.)

Same defect family as the /facilities/in/<country> 404s (backend #3815): a
route that accepts only the canonical spelling of an identifier a machine will
reasonably guess in another form.
"""
import logging

import pytest
from flask import Flask

logging.disable(logging.CRITICAL)
from routes.dcpi import dcpi_bp


@pytest.fixture(scope="module")
def client():
    app = Flask(__name__)
    app.register_blueprint(dcpi_bp)
    return app.test_client()


# The seven measured above. Regression anchors, not examples.
@pytest.mark.parametrize(
    "mixed,canonical",
    [("Ashburn", "ashburn"), ("Santa-Clara", "santa-clara"), ("Columbus", "columbus"),
     ("Phoenix", "phoenix"), ("Dallas", "dallas"), ("Atlanta", "atlanta"),
     ("Chicago", "chicago"), ("ASHBURN", "ashburn"), ("SaNtA-clArA", "santa-clara")],
)
def test_mixed_case_301s_to_the_canonical_lowercase_slug(client, mixed, canonical):
    r = client.get(f"/dcpi/{mixed}")
    assert r.status_code == 301, f"/dcpi/{mixed} returned {r.status_code}, expected 301"
    assert r.headers["Location"].endswith(f"/dcpi/{canonical}")


def test_an_already_canonical_slug_is_not_redirected(client):
    """A self-redirect here would be an infinite loop, not a tidy-up."""
    r = client.get("/dcpi/ashburn")
    assert r.status_code != 301, "canonical slug must be served, not bounced"


def test_case_and_periods_resolve_in_ONE_hop(client):
    """The reason case is folded into the existing normalize_periods 301.

    Handled as two separate redirects, 'St.-Louis' would bounce case -> periods.
    A redirect chain is not a correctness bug but it is a crawl-budget one, and
    the route's own comments already record fighting redirect-into-404s.
    """
    r = client.get("/dcpi/St.-Louis")
    assert r.status_code == 301
    assert r.headers["Location"].endswith("/dcpi/st-louis"), r.headers["Location"]
