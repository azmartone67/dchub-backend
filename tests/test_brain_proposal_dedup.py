"""
tests/test_brain_proposal_dedup.py — STATEFUL proposal dedup (2026-07-16).

NO DB, NO network, NO main import. Covers:
  · condition_fingerprint collapses drifting live counts (the exact failure:
    '4923 verified vs 21958' and '4924 verified vs 21959' never matched the
    old exact-title dedup) and distinguishes different conditions;
  · the shared re-fire rule (should_skip_redraft): open duplicate / cooldown /
    unchanged-after-cooldown skip, material move or no-figures re-draft;
  · the kill switch (BRAIN_PROPOSAL_DEDUP=0) and the cooldown env;
  · propose_enhancements SKIPS drafting (never calls investigate) for a
    suppressed condition, spends the slot on the next fresh one, and reports
    dup_suppressed;
  · pick_agenda_item drops a candidate with an open same-fingerprint agenda
    row and stamps survivors with their fingerprint.
"""
import pytest

dd = pytest.importorskip("routes.brain_proposal_dedup")


# ════════════════════════════════════════════════════════════════════
#  condition_fingerprint — the condition identity
# ════════════════════════════════════════════════════════════════════
def test_fingerprint_collapses_drifting_counts():
    a = dd.condition_fingerprint(
        "data_coverage",
        "canonical_stats: 4923 verified vs 21958 tracked facilities "
        "(17035 in the unverified discovery pile)")
    b = dd.condition_fingerprint(
        "data_coverage",
        "canonical_stats: 4,924 verified vs 21,959 tracked facilities "
        "(17035 in the unverified discovery pile)")
    assert a == b


def test_fingerprint_distinguishes_conditions():
    base = "brain_findings: data_freshness_sla_breach @ table:usgs_water_stress (seen x2847)"
    other = "brain_findings: enterprise_bot_present @ ip_hash=efcc85852e77 (seen x639)"
    assert dd.condition_fingerprint("reliability", base) != \
        dd.condition_fingerprint("reliability", other)
    # Same signal, different area -> different condition.
    assert dd.condition_fingerprint("reliability", base) != \
        dd.condition_fingerprint("performance", base)


def test_fingerprint_falls_back_to_question():
    fp1 = dd.condition_fingerprint("data_coverage", "", "Where should we verify 100 sites first?")
    fp2 = dd.condition_fingerprint("data_coverage", None, "Where should we verify 250 sites first?")
    assert fp1 == fp2  # numbers stripped in the fallback too
    assert fp1


# ════════════════════════════════════════════════════════════════════
#  materially_changed — the re-fire value test
# ════════════════════════════════════════════════════════════════════
def test_materially_changed_tristate():
    prev = "4923 verified vs 21958 tracked"
    # <20% drift -> False (not material).
    assert dd.materially_changed(prev, "4924 verified vs 21959 tracked") is False
    # >20% move on a figure -> True.
    assert dd.materially_changed(prev, "6200 verified vs 21958 tracked") is True
    # Figure-set shape change -> True (state change).
    assert dd.materially_changed(prev, "4923 verified") is True
    # No figures on one side -> None (cooldown alone governs).
    assert dd.materially_changed("no numbers here", prev) is None
    assert dd.materially_changed(prev, "no numbers here") is None


# ════════════════════════════════════════════════════════════════════
#  should_skip_redraft — the shared re-fire rule
# ════════════════════════════════════════════════════════════════════
def test_skip_open_duplicate_inside_cooldown(monkeypatch):
    monkeypatch.delenv("BRAIN_PROPOSAL_REDRAFT_DAYS", raising=False)
    prior = {"age_days": 1.0, "open": True, "text": "4923 verified vs 21958"}
    skip, why = dd.should_skip_redraft(prior, "4924 verified vs 21959")
    assert skip is True and why == "open_duplicate"


def test_skip_closed_prior_inside_cooldown():
    prior = {"age_days": 2.0, "open": False, "text": "4923 verified vs 21958"}
    skip, why = dd.should_skip_redraft(prior, "4924 verified vs 21959")
    assert skip is True and why == "cooldown"


def test_skip_unchanged_after_cooldown():
    prior = {"age_days": 9.0, "open": True, "text": "4923 verified vs 21958"}
    skip, why = dd.should_skip_redraft(prior, "4924 verified vs 21959")
    assert skip is True and why == "unchanged"


def test_redraft_on_material_move_after_cooldown():
    prior = {"age_days": 9.0, "open": True, "text": "4923 verified vs 21958"}
    skip, why = dd.should_skip_redraft(prior, "6200 verified vs 21958")
    assert skip is False and why is None


def test_redraft_after_cooldown_when_no_figures():
    """Value not accessible -> the cooldown ALONE applies."""
    prior = {"age_days": 8.0, "open": True, "text": "baseline present"}
    skip, why = dd.should_skip_redraft(prior, "baseline present")
    assert skip is False


def test_no_prior_drafts():
    assert dd.should_skip_redraft(None, "anything 42") == (False, None)


def test_cooldown_env_override(monkeypatch):
    monkeypatch.setenv("BRAIN_PROPOSAL_REDRAFT_DAYS", "2")
    prior = {"age_days": 3.0, "open": True, "text": "4923 verified"}
    # Past the (shortened) cooldown + unchanged -> still skipped as unchanged.
    skip, why = dd.should_skip_redraft(prior, "4924 verified")
    assert skip is True and why == "unchanged"
    monkeypatch.setenv("BRAIN_PROPOSAL_REDRAFT_DAYS", "7")
    skip, why = dd.should_skip_redraft(prior, "4924 verified")
    assert skip is True and why == "open_duplicate"


def test_kill_switch(monkeypatch):
    monkeypatch.delenv("BRAIN_PROPOSAL_DEDUP", raising=False)
    assert dd.dedup_enabled() is True  # default ON
    monkeypatch.setenv("BRAIN_PROPOSAL_DEDUP", "0")
    assert dd.dedup_enabled() is False


# ════════════════════════════════════════════════════════════════════
#  brain_enhancer.propose_enhancements — skips the DRAFT, not just the store
# ════════════════════════════════════════════════════════════════════
def _wire_enhancer(monkeypatch, opportunities, fp_state):
    enh = pytest.importorskip("routes.brain_enhancer")
    monkeypatch.setattr(enh, "_has_api_key", lambda: True)
    monkeypatch.setattr(enh, "scan_opportunities", lambda: opportunities)
    monkeypatch.setattr(enh, "_proposal_fingerprint_state", lambda: fp_state)

    investigated = []

    def _inv(question, *, depth="default"):
        investigated.append(question)
        return {"question": question, "recommendation": "do X",
                "confidence": 0.7, "caveats": [], "decision_for_human": "d",
                "refutation": {"attempted": True, "survived": True},
                "model": "m"}
    import routes.brain_investigator as bi
    monkeypatch.setattr(bi, "investigate", _inv)
    return enh, investigated


def test_propose_skips_open_duplicate_and_spends_slot_on_next(monkeypatch):
    monkeypatch.delenv("BRAIN_PROPOSAL_DEDUP", raising=False)
    dup = {"area": "data_coverage",
           "signal": "canonical_stats: 4924 verified vs 21959 tracked",
           "question": "verify where first?"}
    fresh = {"area": "reliability",
             "signal": "brain_findings: some brand new condition",
             "question": "fix the new thing?"}
    fp_dup = dd.condition_fingerprint(dup["area"], dup["signal"], dup["question"])
    enh, investigated = _wire_enhancer(
        monkeypatch, [dup, fresh],
        {fp_dup: {"age_days": 0.5, "open": True,
                  "text": "canonical_stats: 4923 verified vs 21958 tracked"}})

    out = enh.propose_enhancements(max_proposals=1)
    # The duplicate NEVER reached investigate (the whole point: no burned
    # draft + adversarial-refutation cycle) — the slot went to the fresh one.
    assert investigated == ["fix the new thing?"]
    assert out["dup_suppressed"] == 1
    assert len(out["proposals"]) == 1
    assert out["proposals"][0]["signal"] == fresh["signal"]
    # The stored candidate carries its fingerprint for the store layer.
    assert out["proposals"][0]["fingerprint"] == dd.condition_fingerprint(
        fresh["area"], fresh["signal"], fresh["question"])


def test_propose_all_duplicates_is_legible(monkeypatch):
    monkeypatch.delenv("BRAIN_PROPOSAL_DEDUP", raising=False)
    dup = {"area": "data_coverage",
           "signal": "canonical_stats: 4924 verified vs 21959 tracked",
           "question": "verify where first?"}
    fp = dd.condition_fingerprint(dup["area"], dup["signal"], dup["question"])
    enh, investigated = _wire_enhancer(
        monkeypatch, [dup],
        {fp: {"age_days": 0.5, "open": True, "text": dup["signal"]}})
    out = enh.propose_enhancements(max_proposals=2)
    assert investigated == []
    assert out["cannot_enhance"] == "all_duplicates_suppressed"
    assert out["dup_suppressed"] == 1


def test_propose_kill_switch_drafts_anyway(monkeypatch):
    monkeypatch.setenv("BRAIN_PROPOSAL_DEDUP", "0")
    dup = {"area": "data_coverage",
           "signal": "canonical_stats: 4924 verified vs 21959 tracked",
           "question": "verify where first?"}
    fp = dd.condition_fingerprint(dup["area"], dup["signal"], dup["question"])
    enh, investigated = _wire_enhancer(
        monkeypatch, [dup],
        {fp: {"age_days": 0.5, "open": True, "text": dup["signal"]}})
    out = enh.propose_enhancements(max_proposals=1)
    assert investigated == ["verify where first?"]
    assert len(out["proposals"]) == 1
    assert out["dup_suppressed"] == 0


# ════════════════════════════════════════════════════════════════════
#  brain_self_director.pick_agenda_item — same rule, agenda table
# ════════════════════════════════════════════════════════════════════
def test_pick_agenda_suppresses_open_condition(monkeypatch):
    monkeypatch.delenv("BRAIN_PROPOSAL_DEDUP", raising=False)
    sd = pytest.importorskip("routes.brain_self_director")
    dup = {"kind": "opportunity", "area": "data_coverage",
           "title": "[data_coverage] 4924 verified vs 21959 tracked facilities",
           "question": "verify where first?", "leverage": 1.5}
    fresh = {"kind": "opportunity", "area": "reliability",
             "title": "[reliability] brand new condition",
             "question": "fix the new thing?", "leverage": 1.0}
    monkeypatch.setattr(sd, "_work_plan_candidates", lambda: [])
    monkeypatch.setattr(sd, "_opportunity_candidates", lambda: [dup, fresh])
    # The short _norm_sig anti-loop window sees nothing recent.
    monkeypatch.setattr(sd, "_recent_sigs_and_lowyield", lambda days: (set(), set()))
    fp_dup = dd.condition_fingerprint(dup["area"], dup["title"], dup["question"])
    monkeypatch.setattr(sd, "_agenda_fingerprint_state", lambda: {
        fp_dup: {"age_days": 4.0, "open": True,
                 "text": "[data_coverage] 4923 verified vs 21958 tracked facilities"},
    })
    picked = sd.pick_agenda_item()
    # The higher-leverage duplicate was suppressed; the fresh one was picked
    # and stamped with its fingerprint.
    assert picked["title"] == fresh["title"]
    assert picked["fingerprint"] == dd.condition_fingerprint(
        fresh["area"], fresh["title"], fresh["question"])


def test_pick_agenda_all_suppressed_skips_tick(monkeypatch):
    monkeypatch.delenv("BRAIN_PROPOSAL_DEDUP", raising=False)
    sd = pytest.importorskip("routes.brain_self_director")
    dup = {"kind": "opportunity", "area": "data_coverage",
           "title": "[data_coverage] 4924 verified vs 21959 tracked facilities",
           "question": "verify where first?", "leverage": 1.5}
    monkeypatch.setattr(sd, "_work_plan_candidates", lambda: [])
    monkeypatch.setattr(sd, "_opportunity_candidates", lambda: [dup])
    monkeypatch.setattr(sd, "_recent_sigs_and_lowyield", lambda days: (set(), set()))
    fp_dup = dd.condition_fingerprint(dup["area"], dup["title"], dup["question"])
    monkeypatch.setattr(sd, "_agenda_fingerprint_state", lambda: {
        fp_dup: {"age_days": 1.0, "open": True, "text": dup["title"]},
    })
    assert sd.pick_agenda_item() is None
