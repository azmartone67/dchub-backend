"""Guards for retiring the redundant DCPI alias twins (r-twin-unpublish, 2026-07-28).

r-twin-dedup (2026-07-19) removed seven redundant slugs from the scoring
universe and its comment said the leftover row "is unpublished separately".
That step did not exist. On 2026-07-28 all seven were still published and
frozen at 2026-07-19 with iso_type NULL — including northern-virginia,
dallas-fort-worth and silicon-valley — while their canonical twins were
recomputed daily. Because the slugs are excluded from MARKETS, no
offset/limit recompute chunk can ever reach them: a full sweep reports
success and leaves them stale forever.

Source-level + pure-function only; never imports the Flask app (routes/dcpi
builds MARKETS at import time, which needs a DB).
"""
import os
import re

import pytest

from util.market_aliases import (
    DCPI_METRO_ALIASES,
    REDUNDANT_TWIN_SLUGS,
    canonical_slug,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    return open(os.path.join(ROOT, rel), encoding="utf-8").read()


# ── the map itself ──────────────────────────────────────────────────────
def test_every_twin_has_a_canonical_target():
    """Retiring a twin with nowhere to redirect would 404 a live URL."""
    assert REDUNDANT_TWIN_SLUGS, "twin set is empty — guard would pass vacuously"
    orphaned = sorted(s for s in REDUNDANT_TWIN_SLUGS if not canonical_slug(s))
    assert not orphaned, f"twins with no canonical target: {orphaned}"


def test_no_twin_points_at_another_twin():
    """A twin redirecting to a retired twin would resolve to an unpublished
    row — the redirect has to land on something still served."""
    chained = sorted(s for s in REDUNDANT_TWIN_SLUGS
                     if canonical_slug(s) in REDUNDANT_TWIN_SLUGS)
    assert not chained, f"twin -> twin redirect chain: {chained}"


def test_the_seven_known_twins_are_tracked():
    """These are the rows found live on 2026-07-28, published and 9 days
    stale. If one is dropped from the set it silently starts being served
    again."""
    assert {"northern-virginia", "dallas-fort-worth", "silicon-valley",
            "cheyenne-wy", "columbus-oh", "the-dalles-or",
            "washington"} <= REDUNDANT_TWIN_SLUGS


# ── resolution: canonical must win even while the twin row exists ───────
def test_canonical_is_tried_before_the_alias_keys_own_row():
    """Unpublishing alone does NOT fix the page.

    Neither /dcpi/<slug> nor /api/v1/dcpi/scores/<slug> filters on
    `published`, so a leftover twin row is still found and served. The fix
    is ordering: the canonical target must be tried first.
    """
    ns = {"DCPI_METRO_ALIASES": DCPI_METRO_ALIASES}
    src = _src("routes/dcpi.py")
    i = src.index("def _canonical_first(")
    body = [src.splitlines()[src[:i].count("\n")]]
    for line in src.splitlines()[src[:i].count("\n") + 1:]:
        if line and not line[0].isspace():
            break
        body.append(line)
    exec(compile("\n".join(body), "dcpi", "exec"), ns)
    _canonical_first = ns["_canonical_first"]

    # the twin's own row exists and is listed first — canonical must overtake it
    out = _canonical_first("northern-virginia", ["northern-virginia", "ashburn"])
    assert out[0] == "ashburn", out
    # a slug that is not an alias key is left completely alone
    assert _canonical_first("dallas", ["dallas"]) == ["dallas"]
    assert _canonical_first("charlotte", ["charlotte"]) == ["charlotte"]
    # degenerate input must not raise
    assert _canonical_first(None, []) == []


@pytest.mark.parametrize("route_marker", [
    'def public_market_page',          # HTML /dcpi/<slug>
    'def api_score_market',            # JSON /api/v1/dcpi/scores/<slug>
])
def test_both_lookup_paths_promote_the_canonical_slug(route_marker):
    """Both resolvers must call _canonical_first, or one surface keeps
    serving the stale twin while the other redirects."""
    src = _src("routes/dcpi.py")
    i = src.index(route_marker)

    # ★ 2026-09-05 — SCAN THE FUNCTION, NOT A FIXED CHARACTER WINDOW. This read
    #   src[i:i+4000] and asserted the call appeared inside it. 4000 chars is a
    #   proxy for "in this function", and the two drift apart the moment anyone
    #   writes a long comment: adding ~20 lines of rationale to
    #   public_market_page pushed the (still present, still called)
    #   _canonical_first to offset 4861 and turned this red, reporting a
    #   behavioural regression that had not happened.
    #
    #   A window that is too SHORT cries wolf; one too LONG is worse — it reads
    #   into the NEXT function and passes because a sibling makes the call. So
    #   widening the number was not the fix. Take the function's real extent:
    #   from its `def` to the next line at column 0.
    lines = src[i:].splitlines()
    end = len(lines)
    for n, line in enumerate(lines[1:], 1):
        if line and not line[0].isspace() and not line.startswith(")"):
            end = n
            break
    body = "\n".join(lines[:end])
    assert len(body) > 200, f"could not delimit {route_marker} — guard is vacuous"
    assert "_canonical_first(" in body, (
        f"{route_marker} does not promote the canonical slug — a leftover "
        "twin row will shadow the redirect on this surface"
    )


# ── retirement must actually stick ──────────────────────────────────────
def test_recompute_retires_twins():
    src = _src("routes/dcpi.py")
    assert "r-twin-unpublish" in src
    assert "SET published = false" in src, (
        "the recompute no longer retires alias twins — they will drift back "
        "to published and frozen"
    )
    # It must only retire a twin whose canonical row still exists.
    i = src.index("SET published = false")
    assert "EXISTS" in src[i:i + 700], (
        "retirement lost its canonical-exists guard — this could unpublish a "
        "market with nowhere to redirect"
    )


def test_self_heal_cannot_republish_a_retired_twin():
    """dchub_self_heal decides `published` on every row, so without the twin
    rule the recompute unpublishes a twin and this job flips it straight back
    — they flap depending on which ran last.

    r-publish-gate (2026-08-08): this used to assert that the curated branch
    carried a `market_slug <> ALL(%s)` exclusion. It did — and the gate's
    OTHER publish statement, the lite-pro one, did not. `washington` is the
    one twin with tier_required='lite-pro', so it flapped for eleven days
    while this test stayed green: the guard was written against one of the
    two statements that could publish.

    The two are now one statement behind util.dcpi_score_row.MAY_PUBLISH, and
    the census in tests/test_dcpi_publish_gate.py is what pins that there is
    no second one to forget. Kept here, pointed at the fence, so this file
    still fails if the retirement stops sticking.
    """
    heal = _src("dchub_self_heal.py")
    assert "MAY_PUBLISH" in heal, (
        "self-heal re-publishes retired twins — the retirement cannot stick"
    )
    assert "may_publish_params()" in heal, (
        "the fence is present but nothing binds the twin slugs to it"
    )
    # imported, never re-listed: a second copy is the bug class from
    # util/iso_taxonomy the same day
    assert "from util.dcpi_score_row import" in heal
    assert "'cheyenne-wy'" not in heal and '"cheyenne-wy"' not in heal, (
        "self-heal hand-copied the twin list instead of importing it"
    )


def test_alias_table_is_not_duplicated_in_routes():
    """routes/dcpi.py must re-export, not redefine."""
    src = _src("routes/dcpi.py")
    assert "from util.market_aliases import DCPI_METRO_ALIASES" in src
    assert not re.search(r"^DCPI_METRO_ALIASES\s*=\s*\{", src, re.M), (
        "routes/dcpi.py redefined the alias table instead of importing it"
    )
