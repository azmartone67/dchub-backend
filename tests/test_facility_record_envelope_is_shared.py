"""One facility row, one envelope, whatever spelling asked for it.

Three routes answered "one facility" and each assembled its own response, so
they served three different free surfaces from the same row. Measured
anonymous, cache-busted, the same minute (2026-09-20, before this change):

    /api/v1/facilities/8484     8 fields, NO coordinates,   _upgrade
    /api/v1/facilities/8484/   10 fields, lat 39.02 (2dp),  _gated
    /api/v1/facility/<slug>    11 fields, lat 33.38 (2dp),  _gated

A trailing slash picked the handler — Werkzeug sorts rules by complexity, so
the bare form reaches get_facility_by_id and the slashed form reaches
facility_by_slug — and the two had drifted apart.

The 8-field one looked like the strictest and was actually the accident:
get_facility_by_id intersected the shared mask with a hardcoded tuple its own
comment called the THIRD copy of the field policy. Serving no coordinates at
all reads as "tighter", but it collapses the rung the ladder exists to create —
under util.facility_tier_gate a free key sharpens 2dp (~1.1 km) to 3dp (~110 m),
and a route that serves no coordinates gives a claimed key nothing to buy.

So these tests pin the LADDER and the SINGLE WRITER, not the field counts: a
count changes whenever the mask does, while "no route builds its own envelope"
is the property that stops the next route re-opening this.
"""
import ast
import os
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

from util.facility_tier_gate import apply_record_gate  # noqa: E402

ROW = {
    'id': 1, 'name': 'N', 'provider': 'P', 'city': 'C', 'state': 'S',
    'country': 'US', 'status': 'operational', 'region': 'R',
    'latitude': 39.022182, 'longitude': -77.457610,
    'power_mw': 50, 'address': 'A', 'source': 'src',
}


# ── the ladder ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tier,dp", [('anon', 2), ('free', 3)])
def test_each_unpaid_rung_gets_its_own_coordinate_precision(tier, dp):
    """anon and free must NOT be identical, or the signup rung buys nothing."""
    r = apply_record_gate({'success': True, 'data': dict(ROW)}, tier)
    assert r['_coord_precision_dp'] == dp
    lat = r['data']['latitude']
    assert lat == round(ROW['latitude'], dp), (
        f"{tier} served {lat}, expected {ROW['latitude']} at {dp}dp")


def test_free_is_strictly_sharper_than_anon():
    """The property the ladder exists for, stated as a comparison rather than
    two hardcoded numbers that could drift together."""
    a = apply_record_gate({'success': True, 'data': dict(ROW)}, 'anon')
    f = apply_record_gate({'success': True, 'data': dict(ROW)}, 'free')
    assert f['_coord_precision_dp'] > a['_coord_precision_dp']
    assert abs(f['data']['latitude'] - ROW['latitude']) < abs(
        a['data']['latitude'] - ROW['latitude']), (
        "a free key must move the caller closer to the true coordinate")
    assert set(f['data']) > set(a['data']), "free must also widen the field set"


def test_paid_is_the_full_record_with_no_markers():
    """`_upgrade` absent is the documented signal that nothing was withheld —
    the curated OpenAPI spec tells third parties to branch on exactly that."""
    r = apply_record_gate({'success': True, 'data': dict(ROW)}, 'developer')
    assert r['data'] == ROW
    for marker in ('_gated', '_upgrade', '_coord_precision_dp', '_redacted_values'):
        assert marker not in r, f"paid response still carries {marker}"


def test_unpaid_always_carries_both_marker_vocabularies():
    """_gated/_coord_precision_dp is what /api/v1/map publishes; _upgrade is what
    the curated spec tells generated connectors to branch on. Dropping either
    breaks a real consumer."""
    for tier in ('anon', 'free'):
        r = apply_record_gate({'success': True, 'data': dict(ROW)}, tier)
        for marker in ('_gated', '_coord_precision_dp', '_redacted_values',
                       '_upgrade', '_upgrade_cta', '_pricing_url'):
            assert marker in r, f"{tier} response is missing {marker}"


def test_the_gate_fails_closed_on_a_hostile_record():
    """A tier-resolution or masking raise must narrow what is served, never
    widen it. An object whose .items() explodes stands in for that."""
    class Exploding(dict):
        def items(self):
            raise RuntimeError("boom")

    bad = Exploding(ROW)
    try:
        r = apply_record_gate({'success': True, 'data': bad}, 'anon')
    except RuntimeError:
        pytest.fail("the envelope propagated the raise instead of failing closed")
    assert 'power_mw' not in r.get('data', {}), "failed OPEN — the full row leaked"


# ── the single writer ───────────────────────────────────────────────────────

def _main_tree():
    return ast.parse((REPO / "main.py").read_text(encoding="utf-8"))


def _func(name):
    for n in ast.walk(_main_tree()):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f"{name} is gone from main.py")


# The per-branch structural check that used to live here is GONE ON PURPOSE.
# tests/test_facility_tier_gate.py::test_every_returned_record_in_a_class_
# handler_is_gated already does it, across every handler in the class rather
# than the two named here, and it carries the measurement that motivated it.
# Two guards for one property is how they drift into disagreeing. What stays
# here is the BEHAVIOUR the structural check cannot see: the ladder, the paid
# signal, failing closed, and not eating what the route already attached.


@pytest.mark.parametrize("fn", ["facility_by_slug", "get_facility_by_id"])
def test_no_route_assembles_its_own_gate_markers(fn):
    """The drift guard. A route that sets _coord_precision_dp itself has
    started a fourth copy of the policy."""
    node = _func(fn)
    literals = {n.value for n in ast.walk(node)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    for marker in ('_coord_precision_dp', '_redacted_values', '_upgrade_cta'):
        assert marker not in literals, (
            f"{fn} assembles {marker} inline instead of calling the shared "
            "envelope — the policy now has two writers again")


def test_the_gate_preserves_what_the_route_already_attached():
    """Provenance is attached BEFORE gating and must survive it.

    Both facility_by_slug branches call routes.provenance.attach_provenance on
    the response first — normalize_coordinates has to see raw values and
    verified_flag() reads `is_duplicate`, which the mask drops. An earlier
    version of the envelope returned a FRESH dict and the call sites assigned
    over theirs, silently dropping that block: the response still looked
    correct field-by-field, and every other test here passed.

    This is the assertion whose absence let that through — mutation-testing
    showed 'gate stops preserving provenance' was vacuous until it existed.
    """
    resp = {
        'success': True,
        'data': dict(ROW),
        'provenance': {'source': 'DC Hub facilities registry'},
        'cite': 'https://dchub.cloud/facilities/x',
    }
    out = apply_record_gate(resp, 'anon')
    assert out.get('provenance') == {'source': 'DC Hub facilities registry'}, (
        "the gate dropped the provenance block the route attached before it")
    assert out.get('cite'), "the gate dropped the route's citation"
    assert out['data']['latitude'] == round(ROW['latitude'], 2), (
        "preserving extras must not have skipped the gating itself")


# ── the key surface the static contract guard can no longer see ─────────────

def test_no_previously_published_key_was_dropped():
    """Replaces the static coverage this endpoint lost.

    contracts/api_response_surface.json recorded GET /api/v1/facilities/
    <facility_id> as `resolved` with these nine keys. Building the response
    through apply_record_gate instead of a dict literal made it `opaque`, so
    the contract guard stopped being able to see it — and an opaque endpoint
    cannot report a removed key. That is how `_user_facing_note` nearly went
    out silently.

    These are the keys the baseline protected. The envelope adds more, which is
    always allowed; what is not allowed is losing one.
    """
    WAS_PUBLISHED = {
        'success', 'data', '_user_facing_note', '_upgrade',
    }
    UPGRADE_SUBKEYS = {'checkout', 'message', 'price', 'tier', 'url'}
    r = apply_record_gate({'success': True, 'data': dict(ROW)}, 'anon')
    missing = WAS_PUBLISHED - set(r)
    assert not missing, (
        f"these keys were in the contract baseline and are gone: {sorted(missing)} "
        "— the endpoint is opaque to the static guard now, so nothing else "
        "would have caught it")
    missing_sub = UPGRADE_SUBKEYS - set(r['_upgrade'])
    assert not missing_sub, f"_upgrade lost subkeys: {sorted(missing_sub)}"
