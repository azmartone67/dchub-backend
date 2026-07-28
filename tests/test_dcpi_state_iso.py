"""r-split-state-iso (2026-07-28) — market_power_scores.iso must name the grid
that actually serves the metro.

Two live defects motivated this file, both found by auditing every DCPI market
record against the physical grid:

  charlotte-nc  reported iso=PJM. Charlotte is Duke Energy Carolinas, which is
                NOT an RTO member — PJM's only North Carolina footprint is
                Dominion's northeastern service area. PJM is a real RTO, so
                consumers took the value literally and pulled PJM
                interconnection-queue projects for a Duke grid.
  kansas-city   reported iso=MISO. Kansas City is Evergy Metro (formerly
                KCP&L), an SPP member. _MARKETS_HARDCODED had carried the
                correct SPP tuple all along; the dynamic loader's
                state-derived value was overwriting it on every recompute.

The two need DIFFERENT fixes, which is the point of this file:
  · NC was a plain error in the state map (Duke dominates NC), so it is fixed
    at the state level and every NC market benefits.
  · MO is genuinely SPLIT — Kansas City is SPP, St. Louis is Ameren Missouri
    and MISO. No state value serves both, so Kansas City needs a per-market
    override and St. Louis must keep the state default.

Pure-function tests: no Flask app, no DB, no network (routes.dcpi is imported
for its module-level maps only).
"""
import re
import pathlib

import pytest

from routes.dcpi import _MARKET_ISO_OVERRIDES, _state_to_iso

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _resolve(slug, state):
    """Mirror the one line in _load_markets_dynamic that assigns iso, so the
    precedence (override beats state default) is asserted, not assumed."""
    return _MARKET_ISO_OVERRIDES.get(slug) or _state_to_iso(state)


class TestCharlotte:
    def test_north_carolina_is_not_pjm(self):
        # The whole defect in one line.
        assert _state_to_iso("NC") == "SERC"
        assert _state_to_iso("NC") != "PJM"

    def test_charlotte_resolves_to_serc(self):
        assert _resolve("charlotte", "NC") == "SERC"

    def test_fixed_at_state_level_so_every_nc_market_benefits(self):
        # Raleigh/Durham are Duke Energy Progress — same grid, same answer.
        # This is why NC was NOT fixed with a per-market override.
        for slug in ("raleigh", "durham", "greensboro"):
            assert _resolve(slug, "NC") == "SERC"
        assert "charlotte" not in _MARKET_ISO_OVERRIDES


class TestKansasCity:
    def test_kansas_city_resolves_to_spp(self):
        assert _resolve("kansas-city", "MO") == "SPP"

    def test_override_beats_the_state_default(self):
        # The override exists precisely because the state default disagrees.
        assert _MARKET_ISO_OVERRIDES["kansas-city"] == "SPP"
        assert _state_to_iso("MO") == "MISO"

    def test_st_louis_keeps_miso(self):
        # The reason Missouri CANNOT be fixed at the state level: St. Louis is
        # Ameren Missouri, genuinely MISO. Flipping MO to SPP would trade the
        # Kansas City bug for a St. Louis one.
        assert _resolve("st-louis", "MO") == "MISO"

    def test_hardcoded_table_already_agreed(self):
        # _MARKETS_HARDCODED has always had ("kansas-city", ..., "SPP"); the
        # dynamic loader was silently overwriting it. If this assertion fails
        # the two sources of truth have drifted apart again.
        src = (ROOT / "routes" / "dcpi.py").read_text()
        row = re.search(r'\(\s*"kansas-city"\s*,[^)]*\)', src)
        assert row, "kansas-city row vanished from _MARKETS_HARDCODED"
        assert '"SPP"' in row.group(0)
        assert _MARKET_ISO_OVERRIDES["kansas-city"] == "SPP"


class TestOverrideTableHygiene:
    def test_every_override_actually_contradicts_its_state(self):
        # An override that merely restates the state default is dead weight and
        # hides the fact that the state map could have been fixed instead.
        states = {"kansas-city": "MO"}
        for slug, iso in _MARKET_ISO_OVERRIDES.items():
            st = states.get(slug)
            assert st, f"{slug} has no state recorded in this test — add it"
            assert iso != _state_to_iso(st), (
                f"{slug} override ({iso}) equals the {st} default; "
                "delete the override or fix the state map"
            )


class TestThreeMapDrift:
    """There are THREE state->ISO maps in this repo. routes/dcpi.py feeds the
    daily recompute and so decides what agents actually see; the other two are
    byte-identical to each other. They still disagree on five states. That is
    recorded here rather than silently reconciled — changing any of them moves
    live markets and deserves its own review. What this test forbids is NEW,
    undocumented drift.
    """

    KNOWN_DIVERGENCES = {
        # state: (routes/dcpi.py, the other two)
        "AL": ("SOCO", "SERC"),   # SOCO is a balancing authority, SERC the region
        "GA": ("SOCO", "SERC"),   # same
        "MI": ("PJM", "MISO"),    # Detroit is DTE/Consumers = MISO; PJM is the
                                  # AEP I&M sliver in the southwest. Looks wrong,
                                  # same shape as the NC bug — not fixed here
                                  # because it was not in scope.
        "SC": ("SOCO", "SERC"),   # Duke / Dominion SC / Santee Cooper, not Southern Co
        "SD": ("MISO", "SPP"),    # genuinely split; neither value is clean
        "MO": ("MISO", "SPP"),    # split: StL=MISO (state default), KC=SPP (override)
    }

    @staticmethod
    def _shared_map():
        src = (ROOT / "scripts" / "bulk_dcpi_score.py").read_text()
        body = src.split("US_STATE_ISO = {")[1].split("}")[0]
        return dict(re.findall(r'"([A-Z]{2})"\s*:\s*"([A-Za-z-]+)"', body))

    def test_nc_and_the_other_maps_now_agree(self):
        # The fix brought dcpi.py into line with the map that was already right.
        assert self._shared_map()["NC"] == _state_to_iso("NC") == "SERC"

    def test_no_undocumented_drift(self):
        shared = self._shared_map()
        drift = {
            st: (_state_to_iso(st), iso)
            for st, iso in shared.items()
            if _state_to_iso(st) and _state_to_iso(st) != iso
        }
        assert drift == self.KNOWN_DIVERGENCES, (
            "state->ISO maps drifted. Either fix the state in routes/dcpi.py "
            "(it writes market_power_scores.iso) or add it to "
            "KNOWN_DIVERGENCES with the utility that justifies it."
        )

    def test_the_two_copies_are_still_identical(self):
        # If these ever diverge there are four maps, not three.
        heal = (ROOT / "dchub_self_heal.py").read_text()
        body = heal.split("US_STATE_ISO = {")[1].split("}")[0]
        assert dict(re.findall(r'"([A-Z]{2})"\s*:\s*"([A-Za-z-]+)"', body)) == self._shared_map()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
