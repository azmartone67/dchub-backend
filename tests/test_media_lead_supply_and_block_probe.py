"""Guards for the 2026-08-24 media cadence collapse.

WHAT BROKE, measured live before these guards existed:

  /api/v1/linkedin-quad/status, by slot_date:
      08-17 4/4  08-19 4/4  08-20 4/4  08-21 2/4  08-22 0/4  08-23 1/4
  successful_7d = 15 against a 21/wk floor. The publisher was HEALTHY the
  whole time (DB token, 57d to expiry; a post shipped 08-24T12:02:30Z with
  its card attached) — this was never the 08-15 outage class.

  /api/v1/brain/media/data-leads returned a board EIGHT leads deep against a
  4-slot/day cadence with MEDIA_ENTITY_WINDOW_DAYS=14, which needs ~56
  distinct entities to sustain. Four lanes each held abundant data and
  emitted exactly ONE lead:

      interconnection  10 operators in by_iso            -> 1
      deal             6 valued rows from _collect_signals -> 1
      tenant           3 rows already SELECTed           -> 1
      new_facility     every fresh site in 24h           -> 1

  and the tenant lane tested its >= 5 facilities bar against rows[0] ALONE,
  so one thin leader disqualified the whole angle (#2722 verbatim).

  Separately, /api/v1/brain/media/editorial-decision?slot=capability returned
  its ONLY candidate annotated `publish_blocked:agentmemory` — from two
  claim-breaker refusals on 08-22 and 08-23 whose COPY BUG WAS ALREADY FIXED
  on 08-23 by #3111/#3117. The block keys on history, so shipping the fix
  bought nothing and the 16:00 slot stayed dark.

Every test that proves a lane ROTATES is paired with a control proving the
old max()/[0] shape produced one lead — a guard that cannot tell the two
apart never protected anything.

Pure: no DB, no network, never imports main.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ── the live 2026-08-24 interconnection snapshot, transcribed from
#    /api/v1/interconnection-queue/snapshot (by_iso, GW of queued load) ──────
_SNAP = {"by_iso": [
    {"iso": "NESO",   "queued_load_total_gw": 600.2},
    {"iso": "ERCOT",  "queued_load_total_gw": 440.3},
    {"iso": "MISO",   "queued_load_total_gw": 222.6},
    {"iso": "SPP",    "queued_load_total_gw": 187.6},
    {"iso": "PJM",    "queued_load_total_gw": 171.0},
    {"iso": "CAISO",  "queued_load_total_gw": 75.0},
    {"iso": "AESO",   "queued_load_total_gw": 25.2},
    {"iso": "IESO",   "queued_load_total_gw": 16.9},
    {"iso": "ISO-NE", "queued_load_total_gw": 14.4},
    {"iso": "NYISO",  "queued_load_total_gw": 10.3},
]}


# ══ 1. the interconnection lane ════════════════════════════════════════════
def test_queue_lane_emits_a_lead_per_operator_not_one_total():
    """★ THE COLLAPSE. Ten operators with real queued load produced ONE lead;
    nine analyst stories were discarded on every run while the desk suppressed
    slots for 'no novel data event'."""
    from routes.media_editorial import _queue_leads_from_snapshot
    leads = _queue_leads_from_snapshot(_SNAP, limit=6)
    # Five, not six: CAISO's 75 GW scores 5.0 against _NEWSWORTHY_MIN=8 and is
    # dropped rather than padded onto the board. See the phantom-depth test.
    assert len(leads) == 5, f"expected 5 above-bar operators, got {len(leads)}"


def test_control_the_single_lead_entry_point_still_returns_exactly_one():
    """★ NEGATIVE CONTROL — the PRE-FIX shape. _queue_lead_from_snapshot is the
    max()-only behaviour the scope tests still pin. If this ever returns a list
    or more than one story, the test above has stopped measuring a change."""
    from routes.media_editorial import _queue_lead_from_snapshot
    one = _queue_lead_from_snapshot(_SNAP)
    assert isinstance(one, dict)
    assert one["dedup_key"] == "queue:neso"      # the max, as before


def test_queue_leads_have_distinct_dedup_keys():
    """Rotation is worthless if the durable (kind, entity) ledger cannot tell
    the leads apart — identical keys would make ten leads behave as one."""
    from routes.media_editorial import _queue_leads_from_snapshot
    keys = [l["dedup_key"] for l in _queue_leads_from_snapshot(_SNAP, limit=10)]
    assert len(keys) == len(set(keys)) >= 5


def test_the_leader_still_ranks_first_after_the_decay():
    """The per-rank decay must not reorder the board — a 600 GW queue outranks
    a 10 GW one no matter where it sits in the rotation."""
    from routes.media_editorial import _queue_leads_from_snapshot
    leads = _queue_leads_from_snapshot(_SNAP, limit=10)
    scores = [l["score"] for l in leads]
    assert scores == sorted(scores, reverse=True)
    assert leads[0]["dedup_key"] == "queue:neso"


def test_the_lane_never_reports_phantom_depth():
    """★ A ROTATION WHOSE RUNNERS-UP FALL BELOW _NEWSWORTHY_MIN IS NOT A
    ROTATION — those leads are filtered straight back out, so the board only
    LOOKS deeper. Measured: the g/12 divisor was tuned when this lane emitted
    the max alone, and CAISO's real 75 GW queue scores 5.0 at rank 5. The lane
    drops them; it must never pad the score to clear a bar they do not clear."""
    from routes.media_editorial import _queue_leads_from_snapshot, _NEWSWORTHY_MIN
    leads = _queue_leads_from_snapshot(_SNAP, limit=10)
    weak = [l["dedup_key"] for l in leads if l["score"] < _NEWSWORTHY_MIN]
    assert not weak, f"below the bar and therefore invisible to the desk: {weak}"
    assert len(leads) >= 4, "the rotation must still be worth several real leads"
    assert "queue:caiso" not in {l["dedup_key"] for l in leads}


def test_scope_rules_survive_the_rotation():
    """★ THE 609 GW REGRESSION CONTROL (2026-07-17, post 100292). NESO is GB:
    it must get a region label and NO share clause. Pairing a GB operator's GW
    with a GB+US denominator is how '35% of all US queued load' got published.
    The rotation must apply this PER ROW, not just to the leader."""
    from routes.media_editorial import _queue_leads_from_snapshot
    leads = {l["dedup_key"]: l for l in _queue_leads_from_snapshot(_SNAP, limit=10)}
    neso = leads["queue:neso"]
    assert neso["queue_scope"] != "US"
    assert "% of" not in neso["headline_number"], "a non-US operator must carry no share clause"
    ercot = leads["queue:ercot"]
    assert ercot["queue_scope"] == "US"
    # US denominator = US rows ONLY. NESO (GB) 600.2, AESO (CA) 25.2 and
    # IESO (CA) 16.9 must all be excluded; every US row must be included.
    _us = 440.3 + 222.6 + 187.6 + 171.0 + 75.0 + 14.4 + 10.3
    assert abs(ercot["queue_scope_total_gw"] - _us) < 0.1, (
        "the US share denominator drifted — mixing scopes is how 609 GW "
        "became '35%% of all US queued load'")


def test_queue_lane_is_defensive_on_junk():
    from routes.media_editorial import _queue_leads_from_snapshot
    assert _queue_leads_from_snapshot({}) == []
    assert _queue_leads_from_snapshot({"by_iso": []}) == []
    assert _queue_leads_from_snapshot({"by_iso": [{"iso": "X"}]}) == []


# ══ 2. the deal lane ═══════════════════════════════════════════════════════
_DEALS = [
    {"buyer": "KKR",       "seller": "CyrusOne", "value_m": 10000},
    {"buyer": "Blackstone","seller": "QTS",      "value_m": 6000},
    {"buyer": "Ares",      "seller": "",         "value_m": 1200},
    {"buyer": "DigitalBridge","seller": "Switch","value_m": 800},
    {"buyer": "Brookfield","seller": "Compass",  "value_m": 400},
    {"buyer": "NoValue",   "seller": "Skip",     "value_m": None},
]


def test_deal_lane_rotates_instead_of_taking_the_max_only():
    """★ THE COLLAPSE. Six valued rows were fetched and one lead emitted, so
    the deal angle went quiet for days whenever that buyer/seller pair was
    inside its entity window."""
    from routes.media_editorial import _deal_leads
    leads = _deal_leads(_DEALS, limit=4)
    assert len(leads) == 4
    assert [l["dedup_key"] for l in leads][0] == "deal:kkr:cyrusone"


def test_control_a_limit_of_one_reproduces_the_old_single_lead():
    """★ NEGATIVE CONTROL. limit=1 is the pre-fix behaviour exactly; if the
    rotation test above passed for any reason other than the rotation, this
    would not differ from it."""
    from routes.media_editorial import _deal_leads
    assert len(_deal_leads(_DEALS, limit=1)) == 1


def test_valueless_deals_are_skipped_not_rendered_as_zero():
    from routes.media_editorial import _deal_leads
    keys = [l["dedup_key"] for l in _deal_leads(_DEALS, limit=6)]
    assert "deal:novalue:skip" not in keys


def test_only_the_top_deal_claims_the_superlative():
    """★ A RUNNER-UP INHERITING 'the largest ... this week' IS A LIE, and the
    claim-breaker gate would refuse it — burning the very slot the rotation
    just bought."""
    from routes.media_editorial import _deal_leads
    leads = _deal_leads(_DEALS, limit=4)
    assert "the largest disclosed DC deal" in leads[0]["trend"]
    for l in leads[1:]:
        assert "the largest disclosed DC deal" not in l["trend"]


# ══ 3. the tenant lane ═════════════════════════════════════════════════════
def test_a_thin_leader_no_longer_disqualifies_the_whole_tenant_angle():
    """★ #2722 VERBATIM: A THRESHOLD MUST DISQUALIFY A CANDIDATE, NOT THE
    ANGLE. The >= 5 facilities bar used to be tested against rows[0] alone, so
    a 3-facility leader silenced two perfectly good tenant leads behind it."""
    from routes.media_editorial import _tenant_leads
    rows = [("TinyCo", 3, 0), ("Amazon Web Services", 40, 900), ("Equinix", 12, 300)]
    leads = _tenant_leads(rows, limit=4)
    assert [l["dedup_key"] for l in leads] == [
        "tenant:top:amazon web services", "tenant:top:equinix"]


def test_control_the_old_bar_on_row_zero_would_have_emitted_nothing():
    """★ NEGATIVE CONTROL, stated as the arithmetic the old code ran: bar
    applied to rows[0] only. If this ever stops describing a silent angle, the
    test above is no longer pinning the fix."""
    rows = [("TinyCo", 3, 0), ("Amazon Web Services", 40, 900), ("Equinix", 12, 300)]
    old_would_emit = 1 if int(rows[0][1]) >= 5 else 0
    assert old_would_emit == 0


def test_only_the_top_tenant_claims_most_tracked():
    from routes.media_editorial import _tenant_leads
    rows = [("Amazon Web Services", 40, 900), ("Equinix", 12, 300), ("Digital Realty", 9, 0)]
    leads = _tenant_leads(rows, limit=3)
    assert "most-tracked" in leads[0]["headline_number"]
    for l in leads[1:]:
        assert "most-tracked" not in l["headline_number"]


def test_tenant_lane_is_defensive_on_junk():
    from routes.media_editorial import _tenant_leads
    assert _tenant_leads([]) == []
    assert _tenant_leads([(None, 40, 0)]) == []
    assert _tenant_leads([("AllThin", 2, 0), ("AlsoThin", 1, 0)]) == []


# ══ 4. the new-facility lane ═══════════════════════════════════════════════
def test_facility_lane_rotates_top_n_by_mw():
    from routes.media_editorial import _new_facility_leads
    facs = [{"name": "Alpha", "mw": 40, "state": "TX"},
            {"name": "Beta",  "mw": 120, "state": "VA"},
            {"name": "Gamma", "mw": 80, "state": "AZ"},
            {"name": "NoMW",  "mw": None}]
    leads = _new_facility_leads(facs, limit=3)
    assert [l["dedup_key"] for l in leads] == [
        "facility:beta", "facility:gamma", "facility:alpha"]
    assert len(_new_facility_leads(facs, limit=1)) == 1     # negative control


# ══ 5. every rotated lead must still be PUBLISHABLE ════════════════════════
def test_every_rotated_lead_leads_with_a_number():
    """★ THE FIX BUYS NOTHING IF THE NEW LEADS CANNOT PUBLISH.
    rank_data_events drops any lead whose headline fails leads_with_number,
    and _should_skip_publish refuses it downstream. A rotation of
    non-compliant leads is a longer board and the same silence."""
    from routes.media_editorial import (leads_with_number, _queue_leads_from_snapshot,
                                        _deal_leads, _tenant_leads, _new_facility_leads)
    every = (_queue_leads_from_snapshot(_SNAP, limit=10)
             + _deal_leads(_DEALS, limit=4)
             + _tenant_leads([("Amazon Web Services", 40, 900), ("Equinix", 12, 300)], limit=2)
             + _new_facility_leads([{"name": "Beta", "mw": 120, "state": "VA"}], limit=1))
    assert len(every) >= 12, "fixture drifted — this must exercise every lane"
    bad = [l["headline_number"] for l in every
           if not leads_with_number(l["headline_number"])]
    assert not bad, f"these would be dropped by the desk's own filter: {bad}"


# ══ 6. the publish-block probe ═════════════════════════════════════════════
_BLOCKED_ONLY = [
    {"kind": "cap_agent_memory", "dedup_key": "cap:agent_memory",
     "score": 62.0, "raw_score": 62.0},
]


@pytest.fixture()
def med(monkeypatch):
    """media_editorial with every external read stubbed."""
    import routes.media_editorial as m
    for v in ("MEDIA_ENTITY_WINDOW_RELAX_DISABLE",
              "MEDIA_PUBLISH_BLOCK_FEEDBACK_DISABLE",
              "MEDIA_EDITORIAL_REST_DAYS"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(m, "_conn", lambda: None)
    monkeypatch.setattr(m, "_recently_posted_keys", lambda **k: set())
    monkeypatch.setattr(m, "_topic_mix_weights", lambda: {})
    monkeypatch.setattr(m, "_semantic_repeat_predicate",
                        lambda ranked: (lambda lead: False))
    monkeypatch.setattr(m, "recent_lead_ledger", lambda **k: [])
    monkeypatch.setattr(m, "recent_publish_blocked_keys", lambda **k: {"agentmemory"})
    monkeypatch.setattr(m, "publish_block_probe_keys", lambda **k: set())
    monkeypatch.setattr(m, "rank_data_events", lambda: [dict(x) for x in _BLOCKED_ONLY])
    return m


def test_the_live_0824_capability_slot_goes_dark_without_a_probe(med, monkeypatch):
    """★ THE MEASURED STATE. One candidate, blocked by two refusals whose copy
    bug was ALREADY FIXED. Nothing rested long enough yet, so the slot is
    silent — this is the control the probe has to beat."""
    out = med.editorial_decision(slot="capability")
    assert out["post"] is False
    assert out["publish_block_probe"] is False
    assert out["ranked"][0]["_novelty"] == "publish_blocked:agentmemory"


def test_a_rested_blocked_lead_is_re_tested_instead_of_the_slot_going_silent(med, monkeypatch):
    """★ THE INVALIDATION EDGE. Same board, same block — the only difference is
    that the refusal has now rested past MEDIA_PUBLISH_BLOCK_PROBE_HOURS. The
    loop could previously learn only that a lead FAILS, never that it was
    FIXED."""
    monkeypatch.setattr(med, "publish_block_probe_keys", lambda **k: {"agentmemory"})
    out = med.editorial_decision(slot="capability")
    assert out["post"] is True
    assert out["publish_block_probe"] is True
    assert out["lead"]["dedup_key"] == "cap:agent_memory"
    assert out["lead"]["_novelty"] == "publish_block_probe"
    assert "probe" in out["reason"]


def test_the_probe_never_displaces_a_lead_that_would_have_published(med, monkeypatch):
    """★ THE PROPERTY THAT KEEPS THIS FROM BECOMING THE 08-15 DEADLOCK. A probe
    may only ever be spent on a slot that was going to be SILENT. Put one fresh
    lead on the board and the blocked one must not be elected."""
    monkeypatch.setattr(med, "publish_block_probe_keys", lambda **k: {"agentmemory"})
    monkeypatch.setattr(med, "rank_data_events", lambda: [
        dict(_BLOCKED_ONLY[0]),
        {"kind": "interconnection", "dedup_key": "queue:ercot",
         "score": 36.0, "raw_score": 36.0},
    ])
    out = med.editorial_decision(slot="dcpi_mover")
    assert out["post"] is True
    assert out.get("publish_block_probe") is not True
    assert out["lead"]["dedup_key"] == "queue:ercot", \
        "the probe outranked a publishable lead — that is the deadlock returning"


def test_the_probe_still_honors_every_other_novelty_gate(med, monkeypatch):
    """The probe forgives a STALE REFUSAL. It must not forgive repetition: a
    blocked lead that also led a post yesterday is still a repeat."""
    monkeypatch.setattr(med, "publish_block_probe_keys", lambda **k: {"agentmemory"})
    monkeypatch.setattr(med, "recent_lead_ledger", lambda **k: [
        {"kind": "cap_agent_memory", "entity": "agentmemory", "days_ago": 1.0}])
    out = med.editorial_decision(slot="capability")
    assert out["post"] is False
    assert out["publish_block_probe"] is False


def test_the_probe_respects_the_feedback_kill_switch(med, monkeypatch):
    """MEDIA_PUBLISH_BLOCK_FEEDBACK_DISABLE=1 turns the whole feedback loop off;
    the probe is part of that loop and must not keep running underneath it."""
    import routes.media_editorial as m
    monkeypatch.setenv("MEDIA_PUBLISH_BLOCK_FEEDBACK_DISABLE", "1")
    monkeypatch.setattr(m, "_conn", lambda: (_ for _ in ()).throw(AssertionError("must not query")))
    assert m.publish_block_probe_keys() == set()


def test_the_probe_read_fails_closed(med, monkeypatch):
    """★ INVERTED FROM ITS SIBLING ON PURPOSE. recent_publish_blocked_keys
    fails OPEN (a bad read must not dark-hold the feed). The probe must fail
    CLOSED — a bad read handing out probes would re-elect refused leads, which
    is the failure the block exists to prevent."""
    import routes.media_editorial as m

    class _BoomCur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): raise RuntimeError("db exploded")

    class _Boom:
        def cursor(self): return _BoomCur()
        def rollback(self): pass
        def close(self): pass

    monkeypatch.delenv("MEDIA_PUBLISH_BLOCK_FEEDBACK_DISABLE", raising=False)
    monkeypatch.setattr(m, "_conn", lambda: _Boom())
    assert m.publish_block_probe_keys() == set()


def test_the_probe_predicate_does_not_widen_past_gate_refusals():
    """★ THE PREDICATE THAT MUST NOT WIDEN, same rule as its sibling.
    `claimed_in_flight` and LinkedIn 5xx are transient and retryable; a probe
    keyed on them would be re-testing leads that were never refused."""
    import inspect
    from routes import media_editorial as m
    src = inspect.getsource(m.publish_block_probe_keys)
    assert "success = FALSE" in src
    assert "LIKE 'gate:%%'" in src, "literal % must be doubled for psycopg2"
    assert "HAVING MAX(posted_at)" in src, "the rest period must be enforced in SQL"
