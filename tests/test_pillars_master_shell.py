"""Pillars 1-3 master shell — pure-function tests (no Flask app, no DB, no
network). Covers the honesty scanner, continent derivation, and lane logic
that keep the announcement copy safe to broadcast."""
import routes.pillars_master_shell as p


def _live_facts(**over):
    f = {
        "dc_verified": 4922, "dc_tracked": 21957, "countries": 181,
        "markets": 316, "deals_total": 4071, "prov_version": 1,
        "canon_ok": True, "p1_flagship_prov": True,
        "sb": {"ok": True, "ranked_count": 36, "has_jp": True, "has_kr": True,
               "has_br": True, "has_tw": True, "partial_ok": True, "detail": "HTTP 200",
               "continents_ranked": ["North America", "Europe", "Asia", "South America"]},
        "ranked_count": 36, "intl_new_feeds": True,
        "continents_ranked": ["North America", "Europe", "Asia", "South America"],
        "p3_changes_ok": True, "p3_saved_gate": 401,
    }
    f.update(over)
    return f


def test_drafts_are_honesty_clean_on_live_facts():
    d = p._drafts(_live_facts())
    assert p._messaging_violations(d) == []
    # all three pillars referenced + honest tokens present
    blob = "\n".join(str(v) for v in d.values()).lower()
    for tok in ("tracked", "verified", "cc-by"):
        assert tok in blob


def test_banned_scan_catches_verified_as_raw_count():
    bad = {"x": "21,957 verified data centers across the world"}
    viol = p._messaging_violations(bad)
    assert any("verified" in v for v in viol)


def test_banned_scan_catches_brazil_gas_share():
    bad = {"x": "Brazil grid is 42% gas right now — provenance verified tracked CC-BY "
                "japan korea saved sites moved"}
    viol = p._messaging_violations(bad)
    assert any("Brazil gas" in v for v in viol)


def test_banned_scan_catches_partial_feed_ranked():
    bad = {"x": "Singapore ranked #1 — provenance verified tracked CC-BY japan brazil "
                "saved sites verdict moved"}
    viol = p._messaging_violations(bad)
    assert any("partial feed" in v for v in viol)


def test_banned_scan_catches_continent_overclaim():
    bad = {"x": "live grid across seven continents — provenance verified tracked CC-BY "
                "japan brazil saved sites verdict"}
    viol = p._messaging_violations(bad)
    assert any("continent" in v for v in viol)


def test_required_pillars_enforced():
    # honest tokens present but pillar 3 (agent state) missing → flagged
    missing_p3 = {"x": "provenance verified tracked CC-BY japan korea brazil continents"}
    viol = p._messaging_violations(missing_p3)
    assert any("pillar 3" in v for v in viol)


def test_continent_count_drives_draft_wording():
    d3 = p._drafts(_live_facts(continents_ranked=["North America", "Europe", "Asia"]))
    assert "three continents" in d3["linkedin"]
    d4 = p._drafts(_live_facts())
    assert "four continents" in d4["linkedin"]


def test_all_lanes_green_on_live_facts():
    f = _live_facts()
    for _key, _label, fn in p._LANES:
        checks = fn(f)
        decided = [c for c in checks if c["pass"] is not None]
        assert decided, f"{_label} produced no decided checks"
        assert all(c["pass"] for c in decided), f"{_label} not green: {checks}"


def test_pillar_lanes_fail_when_not_live():
    # provenance not stamped
    assert not all(c["pass"] for c in p._lane_p1(_live_facts(prov_version=None)) if c["pass"] is not None)
    # brazil feed missing → pillar 2 fails
    sb = dict(_live_facts()["sb"]); sb["has_br"] = False
    assert not all(c["pass"] for c in p._lane_p2(_live_facts(intl_new_feeds=False, sb=sb))
                   if c["pass"] is not None)
    # saved route not gated (200 instead of 401) → pillar 3 fails
    assert not all(c["pass"] for c in p._lane_p3(_live_facts(p3_saved_gate=200)) if c["pass"] is not None)


def test_fmt_and_num_helpers():
    assert p._fmt(21957) == "21,957"
    assert p._fmt(None) == "—"
    assert p._num("42") == 42
    assert p._num("x") is None
