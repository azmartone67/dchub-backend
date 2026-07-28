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
    seg = src[i:i + 4000]
    assert "_canonical_first(" in seg, (
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
    """dchub_self_heal blanket-publishes every row whose tier_required is not
    'lite-pro'. The twins are tier_required='free', so without an exclusion
    the recompute unpublishes them and this job flips them straight back —
    they flap depending on which ran last."""
    heal = _src("dchub_self_heal.py")
    i = heal.index("SET quality_score = GREATEST(quality_score, 80)")
    stmt = heal[i:i + 500]
    assert "market_slug <> ALL" in stmt, (
        "self-heal re-publishes retired twins — the retirement cannot stick"
    )
    assert "REDUNDANT_TWIN_SLUGS" in heal
    # imported, never re-listed: a second copy is the bug class from
    # util/iso_taxonomy the same day
    assert "from util.market_aliases import" in heal
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
