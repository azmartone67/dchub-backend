"""The daily callout's subject line must count causes, not surfaces.

2026-09-09, the brain's primary alarm:

    [DC Hub brain] daily callout — 4 silent pipelines, 294 open findings

Those four were TWO causes:

  · https://dchub.cloud/press and https://dchub.cloud/dc-hub-media/ are one
    static bake. Both reported 4 days behind — the same lag, twice.
  · bluesky_publish reads social_media_posts WHERE status='published' AND
    <bluesky>; smp_other_publish reads the same table WHERE status='published'
    AND platform <> 'linkedin'. The first is a strict SUBSET of the second, so
    when Bluesky holds the newest non-LinkedIn row both lanes read that row.
    Both reported 110.3h.

An alarm that inflates its own count is one people learn to discount. The fold
is by DECLARED containment (`subset_of` in cadence_sentinel.LANES), never by
matching ages — two independent stalls can coincide, and merging those would
hide a real one.
"""
import pytest

import routes.brain_daily_callout as dc
import routes.cadence_sentinel as cs


def _lane(key, label, subset_of=None, age=110.3):
    return {"key": key, "label": label, "subset_of": subset_of,
            "age_hours": age, "reasons": [f"gap: newest activity {age}h ago"],
            "actuator": f"/admin/cadence-sentinel#{key}"}


BLUESKY = _lane("bluesky_publish", "Bluesky publishes (social_media_posts)",
                subset_of="smp_other_publish")
OTHER = _lane("smp_other_publish", "non-LinkedIn publishes (social_media_posts)")
PRESS_PAGES = [
    {"url": "https://dchub.cloud/press", "lag_days": 4, "stale": True},
    {"url": "https://dchub.cloud/dc-hub-media/", "lag_days": 4, "stale": True},
]


def _digest(monkeypatch, pages, stalled):
    monkeypatch.setattr(dc, "press_surface_report",
                        lambda: {"db_newest": None, "pages": pages, "error": None})
    monkeypatch.setattr(dc, "silent_pipelines",
                        lambda: {"stalled": stalled, "unknown": [],
                                 "lanes_checked": 9, "error": None})
    monkeypatch.setattr(dc, "chronic_top5", lambda: [])
    monkeypatch.setattr(dc, "flow_24h", lambda: {"opened_24h": 52, "resolved_24h": 110, "open_now": 294})
    monkeypatch.setattr(dc, "human_gated", lambda: {})
    d = dc.compose_daily_callout()
    assert isinstance(d, dict) and "subject" in d, sorted(d) if isinstance(d, dict) else type(d)
    d["text"] = dc.render_text(d)
    return d


def test_the_0909_morning_reads_as_two_causes_not_four(monkeypatch):
    d = _digest(monkeypatch, PRESS_PAGES, [OTHER, BLUESKY])
    assert "2 silent pipelines" in d["subject"], d["subject"]
    assert "4 silent pipelines" not in d["subject"], d["subject"]


def test_the_surface_count_is_still_published(monkeypatch):
    """Deduplicating must not delete information — the four surfaces are real."""
    d = _digest(monkeypatch, PRESS_PAGES, [OTHER, BLUESKY])
    assert d["n_surfaces"] == 4
    assert "4 surfaces" in d["subject"], d["subject"]


def test_the_folded_lane_is_still_listed_and_marked(monkeypatch):
    d = _digest(monkeypatch, PRESS_PAGES, [OTHER, BLUESKY])
    text = d.get("text") or ""
    assert "Bluesky publishes" in text, "a folded lane must not vanish"
    assert "one cause, not two" in text, text


def test_a_subset_lane_alone_still_fires(monkeypatch):
    """Twitter or Mastodon can keep smp_other_publish fresh while Bluesky is
    dark. If the superset is NOT stalled, the subset is its own cause."""
    d = _digest(monkeypatch, [], [BLUESKY])
    assert "1 silent pipeline," in d["subject"], d["subject"]


def test_two_independent_lanes_are_not_folded_by_a_matching_age(monkeypatch):
    """The fold is declared, not inferred. Same age, no containment => two."""
    a = _lane("press_generation", "press generation")
    b = _lane("linkedin_publish", "LinkedIn publishes (linkedin_posts)")
    d = _digest(monkeypatch, [], [a, b])
    assert "2 silent pipelines" in d["subject"], d["subject"]


def test_pages_with_different_lags_are_different_causes(monkeypatch):
    d = _digest(monkeypatch, [
        {"url": "https://dchub.cloud/press", "lag_days": 4, "stale": True},
        {"url": "https://dchub.cloud/dc-hub-media/", "lag_days": 11, "stale": True},
    ], [])
    assert "2 silent pipelines" in d["subject"], d["subject"]


def test_the_containment_is_declared_in_the_lane_spec():
    """Floor: if the declaration is dropped, every fold above silently stops
    folding and these tests would still pass on an unfolded two-cause fixture.
    Bind to the real spec."""
    spec = next(l for l in cs.LANES if l["key"] == "bluesky_publish")
    assert spec.get("subset_of") == "smp_other_publish"
    sup = next(l for l in cs.LANES if l["key"] == "smp_other_publish")
    assert "social_media_posts" in spec["text_ts_sql"]
    assert "social_media_posts" in sup["text_ts_sql"], (
        "the containment claim only holds while both read the same table")
