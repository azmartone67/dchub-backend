"""An "automated path" must be one we have OBSERVED land, not one we assumed.

★ THE DEFECT. AUTO_PATH_REGISTRIES held seven registries on the strength of a
comment. Lane (d) EXCLUDES anything in that set from the human-gated issue, on
the promise that something automated will fix it. Measured 2026-09-05, five of
the seven promises were empty — so five registries were excluded from the only
lane that could have helped them:

    mcphive           POST /api/submit           -> 404 (GET and POST)
    cursor_directory  POST /api/plugin-submit    -> 429, Vercel bot wall
    lobehub           assumed re-crawl; does NOT — needs a manual click.
                      Stuck at 74 tools against a live 83.
    glama             connector blurb typed into THEIR db, never re-read
    pulsemcp          submissions AND listing changes paused site-wide

★ ROOT CAUSE: one mechanism doing all the work. r-nofakepush correctly deleted
the speculative refresh webhooks when it found they 404'd — but that left "bump
the manifest and wait for a re-crawl" as the ONLY strategy, and exactly one
registry re-crawls. Smithery's listing is current for that reason, not because
this lane pushed anything.
"""
import pytest

import routes.white_glove_propagation as wg


def test_only_observed_paths_are_automated():
    """The five measured-broken registries must NOT be excluded from lane (d)."""
    assert wg.AUTO_PATH_REGISTRIES == set(wg._AUTO_PATH_VERIFIED)
    for name in ("mcphive", "cursor_directory", "lobehub", "glama", "pulsemcp"):
        assert name not in wg.AUTO_PATH_REGISTRIES, (
            f"{name} is back in the automated set — it would again be excluded "
            f"from the human-gated issue. Reason it was demoted: "
            f"{wg._AUTO_PATH_DEMOTED.get(name)}")


def test_every_demotion_carries_its_evidence():
    """A demotion without a measurement is an opinion, and rots like one."""
    for name, why in wg._AUTO_PATH_DEMOTED.items():
        assert why and len(why) > 20, f"{name} demoted with no evidence"


def test_verified_entries_carry_the_observation_that_earned_them():
    for name, why in wg._AUTO_PATH_VERIFIED.items():
        assert why and len(why) > 20, f"{name} promoted with no observation"


def test_the_two_sets_are_disjoint():
    assert not (set(wg._AUTO_PATH_VERIFIED) & set(wg._AUTO_PATH_DEMOTED))


# ── the probe's status ladder ────────────────────────────────────────────
@pytest.mark.parametrize("code,expected", [
    (404, False),   # mcphive — endpoint gone
    (429, False),   # cursor.directory — bot wall
    (403, False),
    (410, False),
    (200, True),
    (201, True),
    (400, True),    # exists, rejected our probe body
    (401, True),
    (422, True),
])
def test_status_ladder(monkeypatch, code, expected):
    import urllib.request, urllib.error
    def fake(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, code, "x", None, None)
    if code < 400:
        class R:
            status = code
            def __enter__(self): return self
            def __exit__(self, *a): return False
        fake = lambda req, timeout=0: R()
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    assert wg.probe_submit_endpoint("https://example.test/s")["reachable"] is expected


def test_upstream_5xx_is_inconclusive_not_a_demotion(monkeypatch):
    """Their outage must not silently move a working path to human-gated."""
    import urllib.request, urllib.error
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=0: (_ for _ in ()).throw(
                            urllib.error.HTTPError(req.full_url, 503, "x", None, None)))
    r = wg.probe_submit_endpoint("https://example.test/s")
    assert r["reachable"] is None, r


def test_transport_error_is_inconclusive(monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=0: (_ for _ in ()).throw(OSError("boom")))
    assert wg.probe_submit_endpoint("https://example.test/s")["reachable"] is None


# ── the asymmetry: DEMOTE-ONLY ───────────────────────────────────────────
def test_a_passing_probe_never_promotes():
    """THE load-bearing property.

    "The endpoint answered" is not "the listing changed". Promoting on a live
    endpoint is exactly how five registries entered the automated set on a
    promise nobody kept, so apply_endpoint_demotions must only ever REMOVE.
    """
    before = set(wg.AUTO_PATH_REGISTRIES)
    healthy = {"mcphive": {"reachable": True, "status": 200, "note": "alive"},
               "cursor_directory": {"reachable": True, "status": 200, "note": "alive"}}
    try:
        assert wg.apply_endpoint_demotions(healthy) == []
        assert wg.AUTO_PATH_REGISTRIES == before, "a passing probe PROMOTED something"
    finally:
        wg.AUTO_PATH_REGISTRIES.clear(); wg.AUTO_PATH_REGISTRIES.update(before)


def test_a_failing_probe_demotes_a_currently_automated_registry():
    before = set(wg.AUTO_PATH_REGISTRIES)
    try:
        wg.AUTO_PATH_REGISTRIES.add("mcphive")          # pretend it was trusted
        out = wg.apply_endpoint_demotions(
            {"mcphive": {"reachable": False, "status": 404, "note": "gone"}})
        assert out == ["mcphive"]
        assert "mcphive" not in wg.AUTO_PATH_REGISTRIES
    finally:
        wg.AUTO_PATH_REGISTRIES.clear(); wg.AUTO_PATH_REGISTRIES.update(before)


def test_inconclusive_probe_leaves_standing_untouched():
    before = set(wg.AUTO_PATH_REGISTRIES)
    try:
        wg.AUTO_PATH_REGISTRIES.add("mcphive")
        assert wg.apply_endpoint_demotions(
            {"mcphive": {"reachable": None, "status": 503, "note": "5xx"}}) == []
        assert "mcphive" in wg.AUTO_PATH_REGISTRIES
    finally:
        wg.AUTO_PATH_REGISTRIES.clear(); wg.AUTO_PATH_REGISTRIES.update(before)
