"""Every _PUBLIC_FLOOR_SPECS key must actually be measured somewhere.

★ THE BUG THIS ENCODES, found 2026-09-07. `substations` was given a floor spec
on 2026-09-02 so that {canon_substations} could exist — but _query_live() never
populated the key. live_public_floors() skips any key where stat_is_live() is
False (it fails CLOSED, by design), so the spec was UNREACHABLE: the placeholder
resolved to the PIN on every request, for five days, and no test noticed.

That is the worst shape a derivation can take. A pinned value that LOOKS derived
attracts none of the suspicion a hardcoded literal does — nobody re-measures it,
because the mechanism appears to be doing it for them. It also cannot be caught
by comparing the served value to canon: the pin and the live floor round to the
same string most of the time, which is exactly why 127,000+ looked correct.

So this asserts the WIRING, not the value: spec exists => the key is queried.
"""
import ast
import re

SRC_PATH = "canonical_stats.py"
SRC = open(SRC_PATH, encoding="utf-8").read()


def _spec_keys():
    """The stat_key each floor spec depends on — the SECOND element of the tuple,
    not the dict key, because those legitimately differ (facilities ->
    facilities_verified)."""
    m = re.search(r"_PUBLIC_FLOOR_SPECS\s*=\s*\{(.*?)\n\}", SRC, re.S)
    assert m, "_PUBLIC_FLOOR_SPECS not found — this guard is scanning the wrong shape"
    body = m.group(1)
    keys = re.findall(r'"([a-z_]+)":\s*\(\s*"([a-z_]+)"', body)
    assert keys, "no floor specs parsed — the literal shape changed"
    return {pub: stat for pub, stat in keys}


def _live_keys_registered():
    """Every key passed to _live_keys.add(...) anywhere in the module, including
    the loop form `_live_keys.add(_pub_key)` whose argument is a variable — those
    are collected from the loop's literal tuple instead."""
    literal = set(re.findall(r'_live_keys\.add\(\s*["\']([a-z_]+)["\']\s*\)', SRC))
    # loop form: ("key", "SELECT ...") pairs feeding a variable add()
    looped = set(re.findall(r'\(\s*"([a-z_]+)",\s*"SELECT COUNT', SRC))
    return literal | looped


def test_every_floor_spec_key_is_actually_queried():
    specs = _spec_keys()
    live = _live_keys_registered()
    assert len(specs) >= 5, (
        f"only {len(specs)} floor specs parsed — the regex drifted and this "
        "guard is about to pass on almost nothing.")
    assert len(live) >= 5, (
        f"only {len(live)} live keys parsed — same risk, other side.")
    dead = {pub: stat for pub, stat in specs.items() if stat not in live}
    assert not dead, (
        "_PUBLIC_FLOOR_SPECS entries whose stat key is never measured in "
        f"_query_live(): {dead}. Each one is a placeholder that silently resolves "
        "to the PIN forever — stat_is_live() fails closed, so "
        "live_public_floors() skips it and nothing reports a problem. Either add "
        "the query or delete the spec.")


def test_the_new_infrastructure_keys_resolve_and_floor_down():
    """The two keys added 2026-09-07, and the sibling whose spec they revived."""
    import ai_surface_canon as canon
    nums = canon.canon_nums()
    for ph in ("{canon_fiber_routes}", "{canon_transmission_lines}",
               "{canon_substations}"):
        val = nums.get(ph)
        assert val, f"{ph} resolves to empty — a surface using it would serve nothing"
        assert val.endswith("+"), (
            f"{ph} is {val!r}; infrastructure counts publish a FLOOR, never an "
            "exact count. An exact number in prose invites a diff every ingest, "
            "which is how 126,427 became untouchable.")


def test_the_pins_do_not_exceed_their_seeds():
    """A floor above reality is the defect the canon block's history records
    three times. Checked against canonical_stats' OWN seed, which is the
    measurement the pin was floored from — not against a number retyped here."""
    import ai_surface_canon as canon
    import canonical_stats as cs
    for pub_key, ph in (("fiber_routes", "{canon_fiber_routes}"),
                        ("transmission_lines", "{canon_transmission_lines}"),
                        ("substations", "{canon_substations}")):
        seed = int(cs._FALLBACK[pub_key])
        pin = int(re.sub(r"\D", "", canon.canon_nums()[ph]))
        assert 0 < pin <= seed, (
            f"{ph} pins {pin:,} against a seed of {seed:,} — a published floor "
            "must never exceed the measurement it was floored from.")
