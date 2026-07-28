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


class TestMichigan:
    """Same defect shape as NC: the whole state was labelled PJM when PJM
    serves almost none of it."""

    def test_michigan_is_not_pjm(self):
        assert _state_to_iso("MI") == "MISO"
        assert _state_to_iso("MI") != "PJM"

    def test_every_michigan_market_resolves_to_miso(self):
        # The four that exist, all on DTE Electric or Consumers Energy.
        # Probed live 2026-07-28: all four reported iso=PJM before this fix.
        for slug in ("detroit", "southfield", "grand-rapids", "lansing"):
            assert _resolve(slug, "MI") == "MISO"

    def test_no_override_needed_for_the_pjm_sliver(self):
        # PJM's only Michigan footprint is AEP Indiana Michigan Power in the
        # far southwest. No DCPI market sits there — benton-harbor and
        # kalamazoo both 404 — so unlike Missouri this state has no split to
        # carve out. If one is ever added it needs a _MARKET_ISO_OVERRIDES
        # entry of "PJM", and this test is the reminder.
        assert not any(
            slug in _MARKET_ISO_OVERRIDES
            for slug in ("benton-harbor", "kalamazoo", "st-joseph")
        )


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


class TestSplitStateSweep:
    """r-iso-sweep (2026-07-28).

    The six markets fixed before this were found because two of the state->ISO
    maps disagreed. That method is blind by construction: a market in a split
    state where every map AGREES on the (wrong) state default is invisible to
    it. So every metro in a genuinely split state was probed against its actual
    serving utility instead. Uniformly-served states cannot produce an error
    and were skipped.

    Two more turned up, both the HARMFUL direction — an RTO label on a grid
    that is not in that RTO, which is what makes callers pull the wrong queue.
    """

    def test_el_paso_is_wecc_not_ercot(self):
        # El Paso Electric sits in the WESTERN Interconnection. ERCOT stops
        # well short of far-west Texas. Reported ERCOT live on 2026-07-28.
        assert _resolve("el-paso", "TX") == "WECC"
        assert _state_to_iso("TX") == "ERCOT"   # ...and the state default is right for the rest

    def test_sacramento_is_not_caiso(self):
        # SMUD is its own balancing authority inside BANC, not a CAISO member.
        # Reported CAISO live on 2026-07-28.
        assert _resolve("sacramento", "CA") == "WECC"
        assert _state_to_iso("CA") == "CAISO"   # correct for the IOU majority

    def test_both_replaced_an_rto_label_with_a_non_rto_one(self):
        # This is the property that made them worth fixing: the OLD value named
        # a real market with a queue, so consumers acted on it.
        from util.iso_taxonomy import has_interconnection_queue
        for old in ("ERCOT", "CAISO"):
            assert has_interconnection_queue(old)
        for slug in ("el-paso", "sacramento"):
            assert not has_interconnection_queue(_MARKET_ISO_OVERRIDES[slug])

    def test_states_verified_clean_stay_untouched(self):
        # Probed live and already correct — pinned so a future state-map edit
        # cannot quietly move them.
        assert _resolve("indianapolis", "IN") == "MISO"   # AES Indiana
        assert _resolve("south-bend", "IN") == "MISO"     # NIPSCO
        assert _resolve("albuquerque", "NM") == "WECC"    # PNM
        assert _resolve("sioux-falls", "SD") == "MISO"    # Xcel
        assert _resolve("louisville", "KY") == "SERC"     # LG&E, non-RTO
        assert _resolve("los-angeles", "CA") == "CAISO"   # SCE majority

    def test_bare_slug_keys_collide_across_states(self):
        # "riverside" in the override table is Riverside MISSOURI (SPP). A
        # Riverside CALIFORNIA market would silently inherit it. None is
        # scored today, so this is latent — but it is the next instance of
        # this bug class, and keying on (slug, state) is the fix when it
        # becomes real.
        assert _MARKET_ISO_OVERRIDES["riverside"] == "SPP"
        assert _state_to_iso("CA") == "CAISO"
        assert _resolve("riverside", "CA") == "SPP"   # documents the collision


class TestOverrideTableHygiene:
    def test_every_override_actually_contradicts_its_state(self):
        # An override that merely restates the state default is dead weight and
        # hides the fact that the state map could have been fixed instead.
        # r-iso-taxonomy (2026-07-28): the rest of the Evergy Metro Missouri
        # side joined kansas-city here. north-kansas-city was live with
        # iso=MISO on the same wrong state default, so correcting only
        # kansas-city would have left one half of the metro on the wrong grid
        # while the Kansas side (Olathe/Overland Park/Lenexa) sat on SPP.
        # r-iso-sweep (2026-07-28): this used to derive the state as
        # {slug: "MO" for slug in _MARKET_ISO_OVERRIDES} — true when every
        # override was a Kansas City suburb, and a landmine the moment one is
        # not. el-paso (TX) and sacramento (CA) would each have been checked
        # against MISSOURI's default and passed for the wrong reason. States
        # are now recorded explicitly, and an unlisted override is a failure
        # rather than a silent MO assumption.
        states = dict(
            {slug: "MO" for slug in (
                "kansas-city", "north-kansas-city", "lees-summit",
                "blue-springs", "independence", "liberty", "gladstone",
                "raytown", "grandview", "riverside", "belton", "raymore",
            )},
            **{"el-paso": "TX", "sacramento": "CA"},
        )
        missing = set(_MARKET_ISO_OVERRIDES) - set(states)
        assert not missing, (
            f"overrides with no state recorded in this test: {sorted(missing)} — "
            "add them here so the contradiction check is real"
        )
        for slug, iso in _MARKET_ISO_OVERRIDES.items():
            st = states[slug]
            assert iso != _state_to_iso(st), (
                f"{slug} override ({iso}) equals the {st} default; "
                "delete the override or fix the state map"
            )


class TestSingleMapInvariant:
    """Superseded TestThreeMapDrift (r-iso-taxonomy, 2026-07-28).

    That class recorded five states on which the three state->ISO maps
    disagreed, and forbade NEW undocumented drift. There are no longer three
    maps to drift: routes/dcpi.py, dchub_self_heal.py,
    scripts/bulk_dcpi_score.py, routes/brain_data_gatherer.py and
    pipeline_sync.py all resolve through util/iso_taxonomy now, so the
    invariant is enforced by construction rather than by comparison.

    (The earlier count of three was low. brain_data_gatherer held the same
    map as a SQL VALUES literal and pipeline_sync held a partial one that
    said TN->MISO; neither matched a `US_STATE_ISO = {` text search. Six
    copies, five opinions.)

    The five deferred divergences are resolved here, each with the utility
    that decides it:
    """

    RESOLVED_DIVERGENCES = {
        # state: (chosen, why)
        "AL": ("SOCO", "Alabama Power IS Southern Company; BA is more "
                       "specific than the SERC region and needs no change"),
        "GA": ("SOCO", "Georgia Power, same reasoning as AL"),
        "SC": ("SERC", "Dominion Energy SC / Santee Cooper / Duke — NOT "
                       "Southern Company, so SOCO was simply wrong"),
        "SD": ("MISO", "genuinely split; the only SD market scored is Sioux "
                       "Falls, which is Xcel Energy and MISO"),
        "MO": ("MISO", "genuinely split; state stays MISO for St. Louis "
                       "(Ameren) and Kansas City takes a per-market override"),
    }

    def test_each_deferred_divergence_was_decided(self):
        for state, (expected, _why) in self.RESOLVED_DIVERGENCES.items():
            assert _state_to_iso(state) == expected, (
                f"{state} resolves to {_state_to_iso(state)}, expected "
                f"{expected} — see RESOLVED_DIVERGENCES for the reasoning"
            )

    def test_nc_and_mi_kept_the_values_the_other_maps_already_had(self):
        # Neither fix invented a value; both adopted what the correct-but-
        # unreachable maps had said all along.
        assert _state_to_iso("NC") == "SERC"
        assert _state_to_iso("MI") == "MISO"

    def test_the_duplicate_maps_are_gone_not_merely_agreeing(self):
        """The old guard compared copies. Assert there are no copies left."""
        for rel in ("scripts/bulk_dcpi_score.py", "dchub_self_heal.py"):
            src = (ROOT / rel).read_text()
            assert "US_STATE_ISO = {" not in src, (
                f"{rel} redefined US_STATE_ISO — it must import STATE_ISO "
                "from util.iso_taxonomy so the maps cannot drift again"
            )
            assert "iso_taxonomy" in src, f"{rel} no longer delegates"

    def test_dcpi_is_still_the_map_that_writes_the_column(self):
        """The reason routes/dcpi.py mattered most: it feeds the recompute
        that writes market_power_scores.iso. Still true — it just delegates
        now instead of holding its own table."""
        src = (ROOT / "routes" / "dcpi.py").read_text()
        assert "from util.iso_taxonomy import" in src
        assert '"NC":"PJM"' not in src.replace(" ", "")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
