"""Every measured public floor must be reachable from prose.

★ THE RECURRING DEFECT, stated six times in _PUBLIC_FLOOR_SPECS itself: "a
surface that needs a number and has no {canon_*} placeholder to reach it HAS to
hardcode." `substations` proved it (/.well-known/mcp.json carried the literal
"126,427 substations" against a live 127,269), then `dcpi_countries`, then
`news_sources` ("40+ sources" reached ~47 files), then `fiber_routes` and
`transmission_lines` (quick_redirects typed "50,000+ fiber routes, 52,000
transmission lines" against a live 66,699 / 94,633 — the 52,000 being a
SUPERSEDED layer, i.e. drifted in which population it described).

Each was fixed one key at a time. `assets` was the sixth: canonical_stats has
described its seed as the "cold-start seed for {canon_assets}" since
2026-09-19, naming a placeholder that did not exist.

This states the rule over _PUBLIC_FLOOR_SPECS instead, so a key added to the
specs is covered without editing this test — the tuple-shaped omission that
caused the 2026-09-19 canon incident three separate times (_PUBLIC_FLOOR_KEYS,
then #4878's replacement tuple, then a seed guard that named one key where four
applied).
"""
import pytest

import ai_surface_canon as canon
import canonical_stats as cstats


@pytest.mark.parametrize("key", sorted(cstats._PUBLIC_FLOOR_SPECS))
def test_every_floor_spec_key_has_a_canon_placeholder(key):
    nums = canon.canon_nums()
    token = "{canon_%s}" % key
    assert token in nums, (
        "%s is a measured public floor with no %s placeholder, so any surface "
        "naming it has to hardcode the number — the defect _PUBLIC_FLOOR_SPECS "
        "documents six times over" % (key, token)
    )


@pytest.mark.parametrize("key", sorted(cstats._PUBLIC_FLOOR_SPECS))
def test_every_floor_placeholder_renders_a_number(key):
    """A registered placeholder that renders '' is worse than none: canon_text()
    substitutes it silently and the sentence loses its number without failing."""
    value = canon.canon_nums().get("{canon_%s}" % key, "")
    assert value, "{canon_%s} renders empty" % key
    rendered = canon.canon_text("X {canon_%s} Y" % key)
    assert "{canon_" not in rendered, "placeholder survived substitution: %r" % rendered
    assert rendered == "X %s Y" % value


def test_the_assets_placeholder_tracks_the_floor_not_a_literal(monkeypatch):
    """It must read the same derivation as every sibling — live floor first,
    pin only at cold start — rather than restating the number."""
    monkeypatch.setattr(canon, "_live_public_floors", lambda: {"assets": "999,000+"})
    assert canon.canon_nums()["{canon_assets}"] == "999,000+"
    monkeypatch.setattr(canon, "_live_public_floors", dict)      # cold worker
    pinned = (canon.PINNED.get("public") or {}).get("assets")
    assert canon.canon_nums()["{canon_assets}"] == pinned
