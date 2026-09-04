"""Guards for r-suffix-301 (2026-09-03) — a 301 must never point at a 404.

r-period-slug consolidates 'st.-louis' onto 'st-louis' with a blanket
``slug.replace(".", "")``. That also ate the period in a file extension, so a
request for the DATA twin was rewritten into a slug that does not exist:

    /dcpi/northern-virginia.json  -> 301 -> /dcpi/northern-virginiajson  -> 404
    /markets/northern-virginia.JSON -> 301 -> /markets/northern-virginiajson -> 404

Measured on production 2026-09-03, following the redirect: 60/60 sampled
/dcpi/<slug>.json and 50/50 sampled /markets/<slug>.JSON ended in 404, against
200/200 for the same slugs with no suffix.

A redirect into a 404 is worse than a plain 404 — a crawler reads 301 as "this
moved, follow me", so the dead end is recorded as the canonical answer. The
invariant these tests pin is not "this one URL works" but: whatever the route
decides, it must never send a caller to a URL built by gluing an extension onto
a slug.
"""
import flask
import pytest

from util.slug_suffix import KNOWN_SUFFIXES, normalize_periods, split_suffix


@pytest.fixture(scope="module")
def client():
    import routes.dcpi as dcpi
    import routes.market_deep_dive as mdd
    app = flask.Flask(__name__)
    app.register_blueprint(mdd.market_deep_dive_bp)
    app.register_blueprint(dcpi.dcpi_bp)
    return app.test_client()


class TestSplit:
    """The unit the two routes now share."""

    @pytest.mark.parametrize("ext", KNOWN_SUFFIXES)
    def test_every_known_suffix_splits_off(self, ext):
        assert split_suffix(f"northern-virginia.{ext}") == ("northern-virginia", ext)

    def test_the_suffix_match_is_case_insensitive(self):
        # '.JSON' is precisely the case that escaped the /markets/<slug>.json
        # rule, because Werkzeug matches a rule's static text case-sensitively.
        assert split_suffix("northern-virginia.JSON") == ("northern-virginia", "json")

    def test_a_name_period_is_NOT_a_suffix(self):
        # The period r-period-slug exists to consolidate. If this splits, the
        # 'st.-louis' -> 'st-louis' redirect breaks.
        assert split_suffix("st.-louis") == ("st.-louis", "")
        assert normalize_periods("st.-louis") == ("st-louis", "")

    def test_a_trailing_initial_is_NOT_read_as_an_extension(self):
        # Why KNOWN_SUFFIXES is an allowlist and not "whatever follows the last
        # dot": 'washington-d.c' would otherwise split to base 'washington-d'
        # + ext 'c', and the real consolidation to 'washington-dc' would break.
        assert split_suffix("washington-d.c") == ("washington-d.c", "")
        assert normalize_periods("washington-d.c") == ("washington-dc", "")

    def test_an_unknown_extension_is_left_alone(self):
        assert split_suffix("northern-virginia.tar") == ("northern-virginia.tar", "")


# The shape the bug produced: a slug with an extension glued to its tail.
def _is_glued(location, slug_stem="northern-virginia"):
    tail = location.rsplit("/", 1)[-1]
    return any(tail == slug_stem + ext for ext in KNOWN_SUFFIXES)


class TestNoRedirectIntoAGluedSlug:
    """The core invariant, stated over the real routes."""

    # Every suffixed URL that must resolve BY REDIRECT, with the target it
    # must reach. Lowercase /markets/<slug>.json is deliberately absent: that
    # one is SERVED by the market_entity_json twin rather than redirected
    # (pinned separately below, and in test_market_entity_json.py).
    REDIRECTS = [
        ("/markets/northern-virginia.JSON", "/markets/northern-virginia.json"),
        ("/markets/northern-virginia.xml", "/markets/northern-virginia"),
        ("/markets/northern-virginia.csv", "/markets/northern-virginia"),
        ("/markets/northern-virginia.txt", "/markets/northern-virginia"),
        ("/dcpi/northern-virginia.json", "/dcpi/northern-virginia"),
        ("/dcpi/northern-virginia.JSON", "/dcpi/northern-virginia"),
        ("/dcpi/northern-virginia.xml", "/dcpi/northern-virginia"),
        ("/dcpi/northern-virginia.csv", "/dcpi/northern-virginia"),
    ]

    @pytest.mark.parametrize("path,target", REDIRECTS)
    def test_a_suffixed_url_never_redirects_to_a_glued_slug(
            self, client, path, target):
        r = client.get(path)
        loc = r.headers.get("Location", "")
        # Assert the 301 explicitly. Without it this test passes VACUOUSLY on
        # the buggy code: the old /dcpi handler called _ensure_tables() before
        # the redirect, so with no DB it returned a 200 fallback carrying no
        # Location at all, and "no glued target" was trivially true. Pinning
        # the status is what makes the DB-free run able to see the defect.
        assert r.status_code == 301, (
            f"{path} -> {r.status_code}, expected a 301; a suffixed URL must "
            "resolve by redirect, not fall through")
        assert not _is_glued(loc), (
            f"{path} -> {r.status_code} {loc} — redirect target glues the "
            "extension onto the slug; that URL 404s")
        assert loc.endswith(target), f"{path} -> {loc}, expected {target}"

    def test_the_lowercase_json_twin_is_served_not_redirected(self, client):
        # The twin route outranks the greedy <slug> rule, so this must NOT be
        # a redirect. (Without a DB the body is the twin's own unknown_market
        # 404; the point here is only that nothing redirected it away.)
        r = client.get("/markets/northern-virginia.json")
        assert r.status_code != 301, (
            "the .json twin was redirected away — the r-entity-json defect")

    def test_the_markets_json_twin_is_the_target_not_the_html_page(self, client):
        # A caller that asked for data must not be handed the HTML page: the
        # lowercase .json URL is served by market_entity_json.
        r = client.get("/markets/northern-virginia.JSON")
        assert r.status_code == 301
        assert r.headers["Location"].endswith("/markets/northern-virginia.json")

    def test_dcpi_drops_the_suffix_onto_the_page(self, client):
        # /dcpi has no .json twin, so the honest target is the page itself —
        # the same place /dcpi/<slug>.html already lands.
        r = client.get("/dcpi/northern-virginia.json")
        assert r.status_code == 301
        assert r.headers["Location"].endswith("/dcpi/northern-virginia")


class TestPeriodConsolidationStillWorks:
    """r-period-slug is the behaviour this fix had to preserve."""

    @pytest.mark.parametrize("prefix", ["/markets", "/dcpi"])
    def test_a_period_slug_still_consolidates(self, client, prefix):
        r = client.get(f"{prefix}/st.-louis")
        assert r.status_code == 301
        assert r.headers["Location"].endswith(f"{prefix}/st-louis")

    # The two surfaces canonicalise in OPPOSITE directions — /markets folds
    # ashburn into northern-virginia, /dcpi folds northern-virginia into
    # ashburn — so each is checked with its own canonical slug.
    @pytest.mark.parametrize("path", ["/markets/northern-virginia",
                                      "/dcpi/ashburn"])
    def test_a_canonical_slug_is_not_redirected(self, client, path):
        r = client.get(path)
        assert r.status_code != 301, "a canonical slug must render, not redirect"
