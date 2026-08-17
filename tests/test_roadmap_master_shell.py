"""Roadmap master shell (#23) — pure-function tests (no Flask app, no DB, no
network). Covers the dup-collapse, timing-sensitivity, and NOW/NEXT/LATER
triage logic that rank the federated backlog."""
import routes.roadmap_master_shell as r


def _prop(**over):
    it = {"id": 1, "title": "add fiber deltas to get_changes", "area": "product",
          "confidence": 0.5, "grade": None, "status": "proposed",
          "created_at": "2026-07-15 00:00:00", "fingerprint": None}
    it.update(over)
    it.setdefault("fp", r._fp_of(it["title"], it["fingerprint"]))
    return it


def _strat(**over):
    g = {"fp": "metered trial wall", "count": 1, "newest": "2026-07-14 00:00:00",
         "title": "Metered trial wall on top-5 paid tools", "latest_status": "new",
         "any_pr_drafted": False, "pr_url": None, "confidence": 0.7}
    g.update(over)
    return g


def _dist(**over):
    w = {"key": "perplexity", "platform": "Perplexity", "score": 40.0,
         "weakest_dim": "discovery", "action": "Publish connector one-pager",
         "owner_gated": False, "effort": "low (doc) + BD", "priority": 48.0,
         "timing_sensitive": False}
    w.update(over)
    return w


# ── _fp_of / _collapse ────────────────────────────────────────────────

def test_fp_prefers_fingerprint_column_then_title_prefix():
    assert r._fp_of("Some Title", "abc123") == "abc123"
    assert r._fp_of("  Some Title  ", None) == "some title"
    assert r._fp_of("  Some Title  ", "   ") == "some title"
    assert len(r._fp_of("x" * 300)) == 120


def test_collapse_groups_dups_and_keeps_first():
    items = [_prop(id=1, confidence=0.5), _prop(id=2, confidence=0.45),
             _prop(id=3, title="something else")]
    out = r._collapse(items)
    assert len(out) == 2
    assert out[0]["id"] == 1 and out[0]["dup_count"] == 2
    assert out[0]["dup_ids"] == [2]
    assert out[1]["dup_count"] == 1


def test_collapse_uses_fingerprint_when_populated():
    a = _prop(id=1, title="title A", fingerprint="fp1")
    b = _prop(id=2, title="totally different title", fingerprint="fp1")
    a["fp"] = r._fp_of(a["title"], a["fingerprint"])
    b["fp"] = r._fp_of(b["title"], b["fingerprint"])
    out = r._collapse([a, b])
    assert len(out) == 1 and out[0]["dup_count"] == 2


# ── timing sensitivity ────────────────────────────────────────────────

def test_timing_sensitive_detection():
    assert r._is_timing_sensitive("medium (timing-sensitive)")
    assert r._is_timing_sensitive("TIMING-SENSITIVE window")
    assert not r._is_timing_sensitive("low (doc) + BD")
    assert not r._is_timing_sensitive(None)


# ── triage ────────────────────────────────────────────────────────────

def test_grade_good_goes_now():
    tri = r._triage([_prop(grade="good", confidence=0.25)], [], [], [])
    assert tri["counts"] == {"now": 1, "next": 0, "later": 0}
    assert tri["now"][0]["source"] == r._SRC_PROPOSALS
    assert "good" in tri["now"][0]["why"]


def test_high_confidence_ungraded_goes_next_rest_later():
    tri = r._triage([_prop(id=1, confidence=0.65, title="hi-conf")],
                    [_prop(id=2, confidence=0.5, title="lo-conf")], [], [])
    assert tri["counts"] == {"now": 0, "next": 1, "later": 1}
    assert tri["next"][0]["title"] == "hi-conf"
    assert tri["later"][0]["source"] == r._SRC_AGENDA


def test_next_threshold_is_inclusive():
    tri = r._triage([_prop(confidence=r._NEXT_CONF)], [], [], [])
    assert tri["counts"]["next"] == 1


def test_pr_drafted_strategic_goes_now_new_goes_later():
    tri = r._triage([], [], [_strat(any_pr_drafted=True, pr_url="https://x/pr/1"),
                             _strat(fp="other", title="other idea")], [])
    assert tri["counts"] == {"now": 1, "next": 0, "later": 1}
    assert tri["now"][0]["source"] == r._SRC_STRATEGIC
    assert tri["now"][0]["pr_url"] == "https://x/pr/1"


def test_timing_sensitive_distribution_goes_now():
    tri = r._triage([], [], [], [
        _dist(key="webmcp", platform="WebMCP", timing_sensitive=True),
        _dist()])
    assert tri["counts"] == {"now": 1, "next": 0, "later": 1}
    assert tri["now"][0]["source"] == r._SRC_DISTRIBUTION
    assert "WebMCP" in tri["now"][0]["title"]


def test_every_triage_item_carries_source_and_why():
    tri = r._triage([_prop(grade="good"), _prop(id=9, confidence=0.7, title="q")],
                    [_prop(id=5, confidence=0.5, title="a")],
                    [_strat(any_pr_drafted=True)], [_dist()])
    for bucket in ("now", "next", "later"):
        for it in tri[bucket]:
            assert it.get("source"), it
            assert it.get("why"), it


def test_now_assembly_order_protects_urgent_items_under_cap():
    # graded-good → timing-sensitive → PR-drafted (the many PR groups must
    # not push the few urgent items past the cap)
    tri = r._triage([_prop(grade="good", title="g")], [],
                    [_strat(any_pr_drafted=True)],
                    [_dist(timing_sensitive=True)])
    assert [it["why"] for it in tri["now"]] == [
        "graded good (accepted idea)",
        "timing-sensitive distribution window",
        "PR drafted — review/merge or close"]


def test_triage_caps_but_counts_stay_true():
    many = [_prop(id=i, title=f"idea {i}", grade="good")
            for i in range(r._TRIAGE_CAP + 5)]
    tri = r._triage(many, [], [], [])
    assert len(tri["now"]) == r._TRIAGE_CAP
    assert tri["counts"]["now"] == r._TRIAGE_CAP + 5


# ── lane helpers ──────────────────────────────────────────────────────

def test_lane_shape_and_status_clamp():
    lane = r._lane("x", "X lane", "some_table", "bogus", [{"a": 1}], "note")
    assert lane["status"] == "warn"  # unknown statuses clamp to warn
    assert lane["count"] == 1 and lane["source"] == "some_table"


def test_shipped_lane_reads_the_approved_store():
    """★2026-07-29: this lane used to assert count == 5 against a hand-typed
    mirror of the static /whats-new cards — and that mirror had already drifted
    (the page shipped six; get_retirement_headroom was never mirrored). The
    lane now reads data/platform_updates.json, the PR-approved store the page
    itself renders, so the count is whatever the owner has approved. Pinning a
    literal here is what let the drift sit unnoticed; assert the CONTRACT."""
    lane = r._lane_shipped(None)  # no DB needed by design
    assert lane["status"] == "pass"
    assert lane["count"] >= 1
    assert lane["source"] == "data/platform_updates.json (PR-approved)"
    # ★2026-08-17: this used to pin two card titles ("Provenance Envelope",
    # "cluster_sites_by_latency"). The owner archived the first one in #2804/
    # #2806 and the lane correctly stopped returning it — so a literal that
    # was only ever a proxy for "the store was really read" turned main red
    # and said nothing true. That is the same mistake the docstring above
    # describes, made one level down. Assert the CONTRACT the lane owes:
    # every card it surfaces is one the owner published, and nothing archived
    # or rejected leaks onto the shipped lane.
    import json as _json
    import os as _os
    _root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    with open(_os.path.join(_root, "data", "platform_updates.json"),
              encoding="utf-8") as _f:
        store = _json.load(_f)
    by_status = {}
    for entry in store.get("updates") or []:
        by_status.setdefault(entry.get("status"), set()).add(entry.get("title"))
    published = by_status.get("published", set())
    assert published, "the store has no published cards — fixture is broken"

    surfaced = {i["title"] for i in lane["items"]}
    assert surfaced, "the shipped lane returned no cards while the store has some"
    leaked = sorted(t for t in surfaced if t not in published)
    assert not leaked, (
        f"the shipped lane surfaced cards the owner has not published: "
        f"{leaked}. A card reaches /whats-new#platform only by a merged PR "
        "setting status='published'; anything else on this lane is either an "
        "archived card leaking back or the static fallback mirror being "
        "served as live truth.")

    # ★ and the OTHER direction, which the leak check alone cannot see. A
    # subset test passes on an empty-ish lane: measured against this test as it
    # stood at #2812, making _lane_shipped drop a card (`cards[:-1]`) kept the
    # suite green, because fewer-than-published is still a subset. Silent
    # under-reporting is the likelier failure of the two — every card the
    # publish gate withholds for a hardcoded figure or a missing field
    # disappears from this lane with no reason attached — so pin the set both
    # ways. MAX_CARDS is the one legitimate reason for the lane to be short.
    from routes.platform_updates import MAX_CARDS
    missing = sorted(published - surfaced)
    assert not missing or len(published) > MAX_CARDS, (
        f"the shipped lane is missing cards the owner published: {missing}. "
        "Either the lane dropped them, or the publish gate withheld an "
        "approved entry (a figure in the prose, a missing field) — in both "
        "cases the roadmap under-reports what shipped and says nothing about "
        f"it. Store published {len(published)}, lane surfaced {len(surfaced)}.")

    assert "merged PR" in lane["note"]
    # Every announced item carries its approval date, never a fabricated one.
    assert all(i.get("announced") for i in lane["items"])


def test_lanes_registry_covers_five_lanes():
    assert [k for k, _fn in r._LANES] == [
        "brain_proposals", "self_agenda", "strategic_recs",
        "distribution", "shipped"]


def test_fmt_conf():
    assert r._fmt_conf(0.456) == "0.46"
    assert r._fmt_conf(None) == "—"
    assert r._fmt_conf("x") == "—"
