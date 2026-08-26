"""2026-08-25 — the manifest check must not invent work that is already done.

WHY THIS EXISTS
---------------
The white-glove run of 2026-08-25 emitted, for registry=smithery:

    "smithery auto-discovers from GitHub README + manifest. Upstream
     manifest is itself stale (manifest missing canon facilities=18,500+)
     — heal it (dchub-mcp-server: node scripts/sync-tools-manifest.mjs -"

Every load-bearing part of that sentence was wrong:

  * the NUMBER contradicted the same run's own payload.canon
    (facilities_floor = 18800), because the detail line read
    `_canonical_numbers()` -> ai_surface_canon.PINNED (the hand-bumped
    DB-DOWN floor) while white-glove's payload resolved canon LIVE;
  * the PREMISE was false — smithery.yaml carried "18,800+ facilities" and
    `node scripts/sync-tools-manifest.mjs` exited 0 with
    "✓ all manifest + facts surfaces consistent". The comparison was an
    exact substring match on a FLOOR, so a manifest AHEAD of the pinned
    number read as missing it;
  * the REMEDIATION was truncated mid-flag by white-glove's [:200] cap, so
    a bare "-" was all an operator saw of "--fix".

The net effect: a lane whose whole job is scrubbing stale numbers off our
listings told an operator to go heal a healthy file, using a number it had
itself got wrong.

The existing closure suite could not catch this: every escalation test
monkeypatches `_upstream_manifest_matches_canon` wholesale, so the
comparison inside it was never exercised against real canon-vs-manifest
values. These tests pin the comparison.

Run:  python3 -m pytest tests/test_upstream_manifest_canon_compare.py -v
"""
from __future__ import annotations

import pytest

from routes import mcp_presence_crawler as pc


class _Resp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status


def _manifest(facilities="18,800+", deals="1,900+"):
    """A manifest in the shape smithery.yaml actually ships: the figures sit
    inside one long prose `description:`, and `deals` is separated from its
    number by an editorial modifier ("tracked M&A")."""
    return (
        "description: \"Real-time data-center intelligence for AI agents: "
        f"{facilities} facilities across 170+ countries, 126,000+ substations, "
        f"a hyperscaler deal tracker, {deals} tracked M&A deals, and daily "
        "DCPI verdicts across 300+ markets.\"\n"
    )


@pytest.fixture
def canon_18800(monkeypatch):
    """Pin canon to the live-resolved phrases, the same origin white-glove's
    payload.canon and the drift detector read."""
    monkeypatch.setattr(pc, "_resolve_canon_public",
                        lambda: {"facilities": "18,800+", "deals": "1,900+"})
    return 18800


# ── the regression: a manifest AHEAD of canon is not stale ────────────
def test_manifest_at_canon_is_not_reported_stale(canon_18800, monkeypatch):
    monkeypatch.setattr(pc.requests, "get", lambda *a, **k: _Resp(_manifest()))
    ok, detail = pc._upstream_manifest_matches_canon()
    assert ok is True, f"manifest carries canon but was called stale: {detail}"


def test_manifest_ahead_of_the_pinned_floor_is_not_reported_stale(monkeypatch):
    """THE 08-25 BUG, exactly. Canon falls back to the PINNED floor (18,500+)
    — which is what happens on any run where the live resolver is degraded —
    while the manifest, generated from /api/v1/canon/phrases, already carries
    18,800+. A floor is a one-sided claim: only an UNDER-claim is stale."""
    monkeypatch.setattr(pc, "_resolve_canon_public", lambda: {})
    monkeypatch.setattr(pc, "_canonical_numbers",
                        lambda: {"facilities": 18500, "deals": 1900})
    monkeypatch.setattr(pc.requests, "get", lambda *a, **k: _Resp(_manifest()))
    ok, detail = pc._upstream_manifest_matches_canon()
    assert ok is True, (
        "a manifest AHEAD of the floor was reported stale — this is the "
        f"exact 08-25 defect: {detail}")


# ── it must still be able to say STALE ────────────────────────────────
def test_manifest_below_canon_is_stale(canon_18800, monkeypatch):
    monkeypatch.setattr(pc.requests, "get",
                        lambda *a, **k: _Resp(_manifest(facilities="12,650+")))
    ok, detail = pc._upstream_manifest_matches_canon()
    assert ok is False, "a genuine under-claim must still route to the heal branch"
    assert "12,650+" in detail and "18,800+" in detail, (
        f"the detail must name BOTH numbers so the remedy is checkable: {detail}")


def test_manifest_missing_the_figure_entirely_is_stale(canon_18800, monkeypatch):
    monkeypatch.setattr(pc.requests, "get",
                        lambda *a, **k: _Resp("description: \"no numbers here\"\n"))
    ok, detail = pc._upstream_manifest_matches_canon()
    assert ok is False, "a manifest that states no figure cannot be carrying canon"


def test_one_stale_figure_is_enough_even_when_the_other_is_ahead(canon_18800, monkeypatch):
    """Every stated figure must clear canon. A manifest that is ahead on
    facilities and stale on deals is still publishing a stale number."""
    monkeypatch.setattr(pc.requests, "get",
                        lambda *a, **k: _Resp(_manifest(facilities="19,000+",
                                                        deals="1,400+")))
    ok, detail = pc._upstream_manifest_matches_canon()
    assert ok is False, f"a stale deals figure must not be masked: {detail}"
    assert "deals" in detail


# ── the invariant that was violated: ONE canon per run ────────────────
def test_detail_never_names_a_figure_the_runs_own_canon_does_not_carry(monkeypatch):
    """The 08-25 row named 18,500+ while the SAME run's payload.canon said
    18800. Whatever number the detail line prints must be the number canon
    resolved for that run — not a second, older origin."""
    monkeypatch.setattr(pc, "_resolve_canon_public",
                        lambda: {"facilities": "18,800+", "deals": "1,900+"})
    # the pinned floor still lags, as it does in production between bumps
    monkeypatch.setattr(pc, "_canonical_numbers",
                        lambda: {"facilities": 18500, "deals": 1900})
    monkeypatch.setattr(pc.requests, "get",
                        lambda *a, **k: _Resp(_manifest(facilities="12,650+")))
    _, detail = pc._upstream_manifest_matches_canon()
    assert "18,800+" in detail, f"detail must quote the LIVE canon: {detail}"
    assert "18,500+" not in detail, (
        f"detail quoted the stale PINNED floor instead of live canon: {detail}")


# ── floor extraction ──────────────────────────────────────────────────
def test_floors_survive_an_editorial_modifier_between_number_and_noun():
    """'1,900+ tracked M&A deals' must resolve to 1900. Requiring adjacency
    would report deals permanently absent, i.e. permanently stale."""
    assert pc._manifest_floors("1,900+ tracked M&A deals", "deals") == [1900]


def test_floors_ignore_fragments_of_a_larger_numeric():
    """A version string must not be read as a figure."""
    assert pc._manifest_floors("version 2.4.4 facilities", "facilities") == []


def test_a_figure_cannot_bridge_past_the_noun_it_belongs_to():
    """The gap between number and noun admits LETTERS only. smithery.yaml packs
    eight figures into one prose sentence, so a gap that admits digits lets an
    unrelated figure reach the noun: here the substation count would be read as
    a facility count, and 126,000 > canon would mask a genuinely stale 18,800.
    This is the case that fails the moment the class is widened to \\w."""
    floors = pc._manifest_floors("126,000+ substations 18800 facilities",
                                 "facilities")
    assert 126000 not in floors, (
        f"the substation figure bridged to 'facilities': {floors}")
    assert floors == [18800]


def test_lowest_stated_floor_wins():
    """A manifest that says 18,800+ in one place and 12,650+ in another is
    still publishing 12,650+, so the comparison must see the low one."""
    text = "18,800+ facilities ... elsewhere 12,650+ facilities"
    assert pc._manifest_floors(text, "facilities")[0] == 12650


def test_a_stale_second_mention_of_the_same_noun_is_not_masked(canon_18800,
                                                               monkeypatch):
    """The VERDICT, not just the extractor. A manifest that states canon in the
    headline and a stale figure further down is still publishing the stale one,
    so the comparison must read the LOWEST stated floor — not the highest, and
    not merely the first."""
    monkeypatch.setattr(pc.requests, "get", lambda *a, **k: _Resp(
        "description: \"18,800+ facilities across 170+ countries.\"\n"
        "longDescription: \"Covering 12,650+ facilities worldwide.\"\n"))
    ok, detail = pc._upstream_manifest_matches_canon()
    assert ok is False, (
        f"a stale second mention was masked by the healthy first one: {detail}")
    assert "12,650+" in detail, f"the detail must name the stale figure: {detail}"


# ── the remediation must reach the operator intact ────────────────────
@pytest.mark.parametrize("matches", [True, False])
def test_next_action_survives_white_gloves_cap_intact(monkeypatch, matches):
    """white_glove_propagation stores next_action truncated. The 08-25 row
    ended '...sync-tools-manifest.mjs -', where a bare '-' reads as the whole
    flag. A truncated INSTRUCTION is a wrong instruction."""
    import re
    import routes.white_glove_propagation as wg
    src = open(wg.__file__, encoding="utf-8").read()
    caps = [int(m) for m in re.findall(r'r\.get\("next_action"\) or ""\)\[:(\d+)\]', src)]
    assert caps, "could not locate white-glove's next_action cap"

    monkeypatch.setattr(pc, "_upstream_manifest_matches_canon",
                        lambda: (matches, "manifest under-claims: facilities "
                                          "12,650+ < canon 18,800+"))
    action = pc._submitter_manifest_refresh("smithery")["next_action"]
    for cap in caps:
        assert len(action) <= cap, (
            f"next_action is {len(action)} chars and the cap is {cap} — the "
            f"operator would read a truncated command: {action[:cap]!r}")
