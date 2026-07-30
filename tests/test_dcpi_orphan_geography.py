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
    assert iso == "", "midrand convention: no registered grid-operator label"
    assert lat < 0 and 27.0 < lon < 29.0, "southern hemisphere ZA, not Mojave"


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
    assert resolve_iso("johannesburg", "GP", default="") == ""
    assert resolve_iso("markham", "ON", default="IESO") == "IESO"


def test_twin_dedup_keeps_both_markets():
    # DCPI_METRO_ALIASES must never alias these slugs away — midrand and
    # johannesburg are distinct Gauteng cities (city-granularity DCPI),
    # markham is distinct from toronto.
    rows = [_hardcoded("johannesburg"), _hardcoded("markham")]
    assert dcpi._dedup_market_twins(list(rows)) == rows
