"""Guards for the 2026-08-24 cross-surface repeat.

WHAT BROKE, measured live on 154 press releases cross-referenced against the
entities the LinkedIn quad actually led with:

    Upper Peninsula MI    LinkedIn 08-19  ·  press 08-21   (2 days)
    Tulsa / Oklahoma SPP  LinkedIn 08-18  ·  press 08-20   (2 days)
    Google $12B deal      LinkedIn 08-20  ·  press 08-20   (SAME DAY)

Neither surface was wrong about itself, which is why neither ever flagged it:

  * press  — media_editorial_gate._COOLDOWN_DAYS = 14, over press_releases
  * quad   — its own (kind, entity) ledger over linkedin_quad_posts
  * and `linkedin_quad_daily.py` / `content_publisher.py` referenced
    editorial_gate and _market_on_cooldown ZERO times.

Two ledgers, no shared state. Each concluded "fresh" and published the same
story to a reader who sees both.

★ The matcher is EXACT-TOKEN, never substring. r86e (2026-06-17) deadlocked the
whole feed for 7 days with greedy substring novelty — a short tail matched
something almost always. The tests below pin BOTH directions of that: the real
collisions must be caught, and "meta" must not be caught by "metadata".

★ The gate must stay RELAXABLE. It is applied in the strict filter only. If it
ever becomes absolute, this is the 2026-08-23 outage again
(test_media_entity_window_relax.py).

Pure: no DB, no network, never imports main.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── the three colliding press releases, transcribed from /api/v1/press-releases ─
_PRESS = [
    {"slug": "upper-michigan-miso-74-excess-power-top-three",
     "title": "Upper Peninsula Michigan Enters Top-Three Build Markets at 74.4 Excess Power—MISO's Cold-Load Corridor"},
    {"slug": "oklahoma-spp-tulsa-okc-build-markets-2026-08-20",
     "title": "Oklahoma's SPP Corridor Breaks into Top-Three Build Markets—Tulsa and OKC Both Cross 75+ Excess"},
    {"slug": "auto-2026-08-20-google-12b-close-gb-queue-600gw",
     "title": "Google's $12.0B Data-Center Transaction Closes While Great Britain's NESO Queue Hits 600 GW"},
]


@pytest.fixture()
def terms():
    from routes.media_editorial import press_terms_from_rows
    return press_terms_from_rows(_PRESS)


# ══ 1. the measured collisions ═════════════════════════════════════════════
@pytest.mark.parametrize("entity,why", [
    ("tulsa",            "exact token in the press SLUG"),
    ("google",           "exact token in slug and title"),
    ("neso",             "exact token in the title"),
    ("upperpeninsulami", "truncated-state tail; prefix of 'upperpeninsulamichigan'"),
])
def test_the_measured_collisions_are_caught(terms, entity, why):
    from routes.media_editorial import cross_surface_hit
    assert cross_surface_hit(entity, terms), f"missed {entity} — {why}"


def test_known_gap_abbreviations_are_not_aliased(terms):
    """★ A LIMIT, PINNED SO IT IS VISIBLE RATHER THAN SILENT.

    The Oklahoma story ran on LinkedIn as entity 'oklahomacity' (08-17) and as
    press three days later — but that release spells it **OKC**, so the matcher
    does not connect them. It is exact-token by design, and teaching it
    'OKC'=='Oklahoma City' means an alias table that will rot.

    In practice that release is still blocked, because the SAME headline also
    says 'Tulsa' and tulsa DOES hit. Cross-surface catches the STORY here, not
    every entity in it. If abbreviation aliasing ever matters on its own, this
    is the test that should start failing on purpose.
    """
    from routes.media_editorial import cross_surface_hit
    assert not cross_surface_hit("oklahomacity", terms)
    assert cross_surface_hit("tulsa", terms), "the story is still caught by its other entity"


def test_control_an_unrelated_entity_is_not_caught(terms):
    """★ NEGATIVE CONTROL. If everything matched, the gate would be a mute
    button rather than a dedup — and the tests above would prove nothing."""
    from routes.media_editorial import cross_surface_hit
    for clean in ("cheyenne", "ashburn", "frankfurt", "singapore", "ercot"):
        assert not cross_surface_hit(clean, terms), f"false positive on {clean}"


# ══ 2. the r86e trap, in both directions ═══════════════════════════════════
def test_a_short_tail_is_not_matched_by_a_longer_word():
    """★★★ THE r86e REGRESSION CONTROL. Greedy substring novelty deadlocked the
    entire feed for 7 days because a short tail matched *something* almost
    always. 'meta' must not be caught by 'metadata'."""
    from routes.media_editorial import press_terms_from_rows, cross_surface_hit
    t = press_terms_from_rows([{"slug": "dchub-metadata-provenance-envelope",
                                "title": "Metadata Provenance For Every Record"}])
    assert not cross_surface_hit("meta", t), "substring matching is back — this is r86e"


def test_but_an_exact_short_token_still_matches():
    """The flip side: 'meta' as its own token IS a real collision, and dropping
    it to be safe would make the gate useless on exactly the operator lane."""
    from routes.media_editorial import press_terms_from_rows, cross_surface_hit
    t = press_terms_from_rows([{"slug": "ferc-fast-track-meta-5000mw-build-timing-shift",
                                "title": "FERC Fast-Track And Meta's 5,000 MW Build"}])
    assert cross_surface_hit("meta", t)


def test_prefix_matching_is_restricted_to_long_tails():
    """Prefix matching exists ONLY for the truncated-state form. Letting it run
    on short tails is substring matching with extra steps."""
    from routes.media_editorial import press_terms_from_rows, cross_surface_hit
    t = press_terms_from_rows([{"slug": "tulsana-county-report", "title": "Tulsana County"}])
    assert not cross_surface_hit("tulsa", t), "'tulsa' prefix-matched 'tulsana'"


def test_junk_inputs_are_safe(terms):
    from routes.media_editorial import cross_surface_hit, press_terms_from_rows
    assert not cross_surface_hit("", terms)
    assert not cross_surface_hit("ab", terms)
    assert not cross_surface_hit("tulsa", set())
    assert press_terms_from_rows([]) == set()
    assert press_terms_from_rows([{}]) == set()


# ══ 3. the desk actually applies it — and can still relax it ═══════════════
_BOARD = [
    {"kind": "dcpi_build", "dedup_key": "build:tulsa", "score": 20.0, "raw_score": 20.0},
]


@pytest.fixture()
def med(monkeypatch):
    import routes.media_editorial as m
    for v in ("MEDIA_ENTITY_WINDOW_RELAX_DISABLE", "MEDIA_PUBLISH_BLOCK_FEEDBACK_DISABLE",
              "MEDIA_EDITORIAL_REST_DAYS", "MEDIA_CROSS_SURFACE_DISABLE"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(m, "_conn", lambda: None)
    monkeypatch.setattr(m, "_recently_posted_keys", lambda **k: set())
    monkeypatch.setattr(m, "_topic_mix_weights", lambda: {})
    monkeypatch.setattr(m, "_semantic_repeat_predicate", lambda ranked: (lambda lead: False))
    monkeypatch.setattr(m, "recent_lead_ledger", lambda **k: [])
    monkeypatch.setattr(m, "recent_publish_blocked_keys", lambda **k: set())
    monkeypatch.setattr(m, "publish_block_probe_keys", lambda **k: set())
    monkeypatch.setattr(m, "rank_data_events", lambda: [dict(x) for x in _BOARD])
    monkeypatch.setattr(m, "recent_press_terms", lambda **k: set())
    return m


def test_the_desk_flags_a_lead_press_already_ran(med, monkeypatch):
    """★ THE 08-18/08-20 TULSA CASE. Press ran it; the quad's own ledger is
    empty, so before this gate the lead scored FRESH."""
    from routes.media_editorial import press_terms_from_rows
    monkeypatch.setattr(med, "recent_press_terms", lambda **k: press_terms_from_rows(_PRESS))
    out = med.editorial_decision(slot="dcpi_mover")
    assert out["ranked"][0]["_novelty"] == "cross_surface_press:tulsa"


def test_control_without_press_terms_the_same_lead_is_fresh(med):
    """★ NEGATIVE CONTROL — the PRE-FIX verdict, reachable by emptying the press
    side. If this ever stops saying 'fresh', the test above is not measuring
    the gate."""
    out = med.editorial_decision(slot="dcpi_mover")
    assert out["ranked"][0]["_novelty"] == "fresh"
    assert out["post"] is True


def test_the_gate_is_relaxable_and_does_not_dark_hold_the_feed(med, monkeypatch):
    """★★★ THE 2026-08-23 REGRESSION CONTROL. A board where EVERY lead collides
    with press must still post via the relaxed rung. Making this absolute
    inside a ladder whose job is preventing deadlock is the outage itself."""
    from routes.media_editorial import press_terms_from_rows
    monkeypatch.setattr(med, "recent_press_terms", lambda **k: press_terms_from_rows(_PRESS))
    out = med.editorial_decision(slot="dcpi_mover")
    assert out["post"] is True, "cross-surface went absolute — the feed just went dark"
    assert out["lead"]["dedup_key"] == "build:tulsa"
    # ★ post:True ALONE DOES NOT PROVE THIS. The stale-rerun rung will also
    #   rescue a lone rested lead, so an absolute cross-surface gate still ends
    #   up posting and the assertion above passes while the fix is reverted —
    #   verified by mutation. `stale_fallback` is what separates the rungs:
    #   the RELAXED rung must be what fired, not the last-ditch one.
    assert out["stale_fallback"] is False, (
        "the relaxed rung did not fire — cross-surface is being treated as "
        "absolute and only the stale-rerun fallback saved the slot")


def test_the_kill_switch_turns_the_whole_edge_off(monkeypatch):
    import routes.media_editorial as m
    monkeypatch.setenv("MEDIA_CROSS_SURFACE_DISABLE", "1")
    monkeypatch.setattr(m, "_conn", lambda: (_ for _ in ()).throw(AssertionError("must not query")))
    assert m.recent_press_terms() == set()


def test_the_press_read_fails_open(monkeypatch):
    """Fail-OPEN, like recent_lead_ledger: a bad read must never dark-hold the
    feed. (Contrast publish_block_probe_keys, which fails CLOSED — handing out
    probes on a bad read would re-elect refused leads.)"""
    import routes.media_editorial as m

    class _BoomCur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): raise RuntimeError("db exploded")

    class _Boom:
        def cursor(self): return _BoomCur()
        def close(self): pass

    monkeypatch.delenv("MEDIA_CROSS_SURFACE_DISABLE", raising=False)
    monkeypatch.setattr(m, "_conn", lambda: _Boom())
    assert m.recent_press_terms() == set()


# ══ 4. the press half ══════════════════════════════════════════════════════
def test_press_gate_sees_what_linkedin_ran(monkeypatch):
    """★ THE MIRROR. Press about Tulsa, two days after LinkedIn led with it."""
    import routes.media_editorial as m
    import routes.media_editorial_gate as g
    monkeypatch.delenv("MEDIA_CROSS_SURFACE_DISABLE", raising=False)
    monkeypatch.setattr(m, "recent_lead_ledger",
                        lambda **k: [{"kind": "dcpi_build", "entity": "tulsa", "days_ago": 2.0}])
    hit = g.press_blocked_by_linkedin("oklahoma-spp-tulsa-okc-build-markets-2026-08-20",
                                      "Oklahoma's SPP Corridor—Tulsa and OKC Cross 75+")
    assert hit == "tulsa"


def test_press_gate_ignores_a_linkedin_post_outside_the_window(monkeypatch):
    import routes.media_editorial as m
    import routes.media_editorial_gate as g
    monkeypatch.delenv("MEDIA_CROSS_SURFACE_DISABLE", raising=False)
    monkeypatch.setattr(m, "recent_lead_ledger",
                        lambda **k: [{"kind": "dcpi_build", "entity": "tulsa", "days_ago": 30.0}])
    assert g.press_blocked_by_linkedin("oklahoma-spp-tulsa-okc-build-markets", "Tulsa") is None


def test_press_gate_fails_open(monkeypatch):
    import routes.media_editorial as m
    import routes.media_editorial_gate as g
    def boom(**k): raise RuntimeError("ledger down")
    monkeypatch.setattr(m, "recent_lead_ledger", boom)
    assert g.press_blocked_by_linkedin("anything", "Anything") is None
