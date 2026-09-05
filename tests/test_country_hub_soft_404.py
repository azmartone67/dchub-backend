"""/facilities/in/<cc> answered 200 for every two-letter string on earth.

r-country-hub-404 (2026-08-27).

Measured against production: all 676 two-letter codes returned HTTP 200 with
`index, follow` and a self-referencing canonical. 498 of them held ZERO
facilities, and 425 of those are not country codes at all —

    /facilities/in/qz   /facilities/in/ww   /facilities/in/oa   /facilities/in/ys

each a ~14 KB near-identical shell reading "Data Centers in QZ". That is an
unbounded indexable space, the same defect class as the /news/ digest that
answered 200 for any slug (frontend #1255), and the same "soft-404" lesson
already written into the US-state branch of this very file:

    # Honest 404 with onward links (never a soft-404 — the /markets lesson).

The state branch does it correctly — /facilities/in/us/zz already 404s. The
country branch was the outlier.

★ 404ing an empty hub cannot break an internal link. The only emitters of
  /facilities/in/<cc> are the facility page's country breadcrumb and its
  "Browse all data centers in X" link, and both use the facility's OWN
  country, which therefore has at least one row in the hub's own query.
  Verified live over the 62 distinct country codes across 500 sampled
  facility pages: none pointed at an empty hub. No /facilities/in/ URL
  appears in any sitemap, and the pages carry no cross-links to each other
  (0 across all 676).

★★ THE CACHE IS THE TRAP. _cached() stores a BODY and _respond() defaults to
   status=200, so caching the 404 body would replay it as a 200 on the next
   request and silently re-open the hole. That is what the second test below
   exists to catch, and it is why the branch does not call _store().
"""
import re

import pytest

pytest.importorskip("flask")


class _Cur:
    def __init__(self, rows): self._rows = rows
    def execute(self, *a, **k): pass
    def fetchone(self): return (1,)          # canonical_slug DDL probe: present
    def fetchall(self): return self._rows
    def close(self): pass


class _Conn:
    def __init__(self, rows): self._rows = rows
    def cursor(self, *a, **k): return _Cur(self._rows)
    def rollback(self): pass
    def close(self): pass


def _client(monkeypatch, rows):
    from flask import Flask

    import facilities_hub as fh

    fh._CACHE.clear()
    monkeypatch.setattr(fh, "_conn", lambda: _Conn(rows))
    app = Flask(__name__)
    app.register_blueprint(fh.facilities_hub_bp)
    return app.test_client(), fh


# (name, provider, grp, city, state, power_mw, canonical_slug)
ONE_ROW = [("Equinix FR5", "Equinix", "Frankfurt", "Frankfurt", "HE", 12.0,
            "equinix-equinix-fr5-1a2b3c4d")]


def test_a_country_with_no_facilities_is_an_honest_404(monkeypatch):
    cl, _ = _client(monkeypatch, [])

    r = cl.get("/facilities/in/qz")

    assert r.status_code == 404, (
        f"/facilities/in/qz answered {r.status_code} — every two-letter string "
        "is an indexable page again, which is 425 near-identical shells")


def test_the_404_is_not_replayed_as_a_200_from_cache(monkeypatch):
    """The trap: _cached() carries a body, not a status.

    If the empty branch ever calls _store(), the SECOND request finds the body
    in the cache and _respond() hands it back with its default status of 200 —
    so the hole reopens on request two and the first test still passes.
    """
    cl, _ = _client(monkeypatch, [])

    first = cl.get("/facilities/in/qz")
    second = cl.get("/facilities/in/qz")

    assert first.status_code == 404
    assert second.status_code == 404, (
        "the second request answered "
        f"{second.status_code} — the 404 body was cached and _respond() "
        "replayed it as a 200")


def test_a_country_that_has_facilities_still_renders(monkeypatch):
    """The guard must not cost a real country its page."""
    cl, _ = _client(monkeypatch, ONE_ROW)

    r = cl.get("/facilities/in/de")

    assert r.status_code == 200, (
        f"a country WITH facilities answered {r.status_code} — the 404 branch "
        "is firing on real hubs and 178 country pages just went dark")
    assert b"Equinix FR5" in r.data, "the facility list did not render"


def test_the_empty_page_does_not_claim_to_be_a_listing(monkeypatch):
    """A 404 titled 'Data Centers in QZ' still reads as a real page.

    Google's soft-404 detector keys on the CONTENT as much as the status, and
    a human landing there should see what happened.
    """
    cl, _ = _client(monkeypatch, [])

    body = cl.get("/facilities/in/qz").data.decode("utf-8", "replace")

    # ★ Assert on the <h1> ALONE. The first version of this test searched the
    #   whole document for "No data centers listed" — a phrase that also lives
    #   in the <title>, so reverting the heading still passed. Mutation
    #   testing caught it; a two-place string is not a discriminator.
    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", body, re.S)
    assert h1, "the empty hub rendered no <h1> at all"
    heading = h1.group(1)

    assert "Data Centers in" not in heading, (
        f"the empty hub's heading is {heading!r} — it still presents itself as "
        "a data-center listing, which is what Google's soft-404 detector reads")
    assert "No data centers listed" in heading, (
        f"the empty hub's heading is {heading!r}, which does not say what "
        "happened")
