"""r-orphan-geography (2026-07-30) — johannesburg is Johannesburg SOUTH
AFRICA (Gauteng), markham is Markham ONTARIO.

THE BUG: both markets were created long ago from mis-countried facility
rows, then kept alive solely by the _load_scored_orphans re-adopter, which
feeds the market_power_scores row's own (state, iso, lat, lon) back into
every recompute. johannesburg carried state='GA' (Gauteng abbreviated —
but US-state-shaped), so ISO normalization stamped iso=SOCO and a
geocode-era backfill placed it at Johannesburg CALIFORNIA (35.37,
-117.63, Mojave Desert); its 80 discovered_facilities rows are ALL
country='ZA'. markham (27 facilities, ALL Ontario) was published as
NY/NYISO at the Markham hamlet in upstate New York.

These tests pin the corrected hardcoded tuples and the guards that must
not re-US-ify them.
"""
import pytest

pytest.importorskip("flask")
pytest.importorskip("psycopg2")

from routes import dcpi  # noqa: E402
from util.iso_taxonomy import STATE_ISO, resolve_iso  # noqa: E402


def _hardcoded(slug):
    rows = [m for m in dcpi._MARKETS_HARDCODED
            if isinstance(m, tuple) and m[0] == slug]
    assert len(rows) == 1, f"{slug} must appear exactly once in _MARKETS_HARDCODED"
    return rows[0]


def test_johannesburg_is_gauteng_not_georgia():
    slug, name, state, iso, lat, lon = _hardcoded("johannesburg")
    assert state == "GP", "Gauteng — never 'GA' (US Georgia collision)"
    assert lat < 0 and 27.0 < lon < 29.0, "southern hemisphere ZA, not Mojave"
    # ★r-failopen-operators (2026-09-04). This line used to read
    #     assert iso == "", "midrand convention: no registered ... label"
    # and it was CHANGED DELIBERATELY, not relaxed. What this file exists to
    # prevent is a US operator being stamped on a South African market — the
    # measured defect was iso=SOCO. The empty string was one way to satisfy
    # that, and it carried a cost nobody priced: an unregistered label fails
    # `bool(iso) and iso in iso_defaults`, so johannesburg published WECC's
    # curtailment_pct 7.5 and reserve_margin_pct 20.0 for five weeks
    # (measured live 2026-09-04 alongside seven sibling markets).
    #
    # ESKOM is the real operator, so it satisfies the original guard AND
    # closes the fail-open. The assertion below is the invariant that was
    # always the point; `== ""` was an implementation detail standing in for it.
    assert iso not in dcpi._US_DCPI_ISOS, (
        "a US grid label on a South African market is the r-orphan-geography "
        "defect itself (it was SOCO)")
    assert iso == "ESKOM", (
        "johannesburg must carry its real operator — an unregistered label "
        "silently reinstates WECC's Western-US anchors via the "
        "iso_defaults.get(iso, iso_defaults['WECC']) fail-open")


def test_markham_is_ontario_not_new_york():
    slug, name, state, iso, lat, lon = _hardcoded("markham")
    assert state == "ON" and iso == "IESO", "Greater Toronto, like toronto/ottawa"
    assert 43.0 < lat < 45.0 and -80.0 < lon < -79.0


def test_us_iso_normalizer_leaves_them_alone():
    rows = [_hardcoded("johannesburg"), _hardcoded("markham")]
    assert dcpi._normalize_us_isos(list(rows)) == rows


def test_state_codes_stay_out_of_the_us_state_map():
    # A future STATE_ISO entry for 'GP' or 'ON' would silently re-stamp
    # these markets with a US grid label on the next recompute — the exact
    # mechanism that produced johannesburg=SOCO.
    assert "GP" not in STATE_ISO
    assert "ON" not in STATE_ISO
    # default= is what the caller would pass; GP is absent from STATE_ISO so
    # the resolver must hand the caller's own value straight back rather than
    # inventing a US label from the state code.
    assert resolve_iso("johannesburg", "GP", default="") == ""
    assert resolve_iso("johannesburg", "GP", default="ESKOM") == "ESKOM"
    assert resolve_iso("markham", "ON", default="IESO") == "IESO"


def test_twin_dedup_keeps_both_markets():
    # DCPI_METRO_ALIASES must never alias these slugs away — midrand and
    # johannesburg are distinct Gauteng cities (city-granularity DCPI),
    # markham is distinct from toronto.
    rows = [_hardcoded("johannesburg"), _hardcoded("markham")]
    assert dcpi._dedup_market_twins(list(rows)) == rows
