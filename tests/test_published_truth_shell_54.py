"""Guard for the Published Truth master shell (#54) — 2026-08-20.

★ WHAT THIS FILE HAS TO PROVE

Shell #54 is BORN RED: all eight lanes were measured FAILING the day it shipped.
That makes the usual "does it pass?" test worthless — it passes nothing. The
only question worth asking is the opposite one:

    can each lane ever go GREEN, and does it go green for the RIGHT reason?

A lane that is red today and structurally incapable of going green is not a
guard, it is a permanent alarm nobody will read. So every lane below is driven
BOTH ways on synthetic payloads: the defect shape (must FAIL) and the fixed
shape (must PASS).

★ THE TRAP THIS FILE ALREADY CAUGHT ONCE. The first draft of
_lane_retention_population searched every note on the payload for the word
"population" and went GREEN — because agent_cohort_note contains it, and
agent_cohort was never the defect. The lane is now scoped to the KEY side and
carries its own control. Same class as the vacuous wiring guard in
test_funnel_flat_wow_comparability.

Pure: no DB, no network, no Flask app. Lanes are driven on injected dicts.
"""
import importlib.util
import os

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC_PATH = os.path.join(_HERE, "routes", "published_truth_master_shell.py")
_spec = importlib.util.spec_from_file_location("_pts54", _SRC_PATH)
_M = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_M)
_SRC = open(_SRC_PATH, encoding="utf-8").read()

_v = _M._lane_verdict
_CORRECTION_WEEK = "2026-08-10"


# ── payload builders ────────────────────────────────────────────────────────
def _funnel(**over):
    d = {
        "real_external_complete_wk_comparability": {
            "superseded_by_correction": True, "quotable_as_trend": False},
        "real_external_calls_complete_wk": 2100,
        "press_headline_metric": (
            "DC Hub served 2,100 external AI-agent tool calls in the week of "
            "2026-08-10 (WoW withheld — every week in this delta predates a "
            "measurement correction); 362,542 external requests since launch."),
        "quota_wall": {"enforce": True, "hits_month": 0, "keys_month": 0,
                       "table_exists": False, "lazily_created": True},
        "real_external_signals_7d": 557,
        "conversions_30d": 6, "conversions_attributed_30d": 6,
        "paid_signal_attribution_30d": {
            "definition": "honest paid = stripe_customer_id NOT NULL, seed/comp/NLR excluded",
            "paid_total": 4, "bridged_to_signal": 1, "unattributable": 3},
        "keys_by_tier": {"identified": 434, "free": 114, "paid": 44, "enterprise": 6},
        "top_caller_client": "Smithery Connect", "top_caller_pct_7d": 39.2,
        "top_caller_note": "Registry gateways are deliberately NOT excluded",
    }
    d.update(over)
    return d


def _deadman(**over):
    d = {"feeds": [
        {"feed": "backup-neon-r2", "status": "latest-run-failed (latest=cancelled)",
         "age_hours": 45.2, "cadence_hours": 30.0, "overdue": True,
         "reasons": ["status=latest-run-failed (latest=cancelled)"]},
        {"feed": "restore-test", "status": "success", "age_hours": 66.4,
         "cadence_hours": 190.0, "overdue": False, "reasons": []},
        {"feed": "iso-lmp-pjm", "status": "success", "age_hours": 0.1,
         "cadence_hours": 3.0, "overdue": False, "reasons": []},
    ]}
    d.update(over)
    return d


def _retention(**over):
    d = {
        "primary_metric": "summary.pct_returned_next_week_mature (durable api_key, mature 8-30d cohort)",
        "agent_cohort": [{"week": "2026-08-10", "new_agents": 64}],
        "agent_cohort_note": "CANONICAL GRAIN (mcp_calls_identity.agent_id) — the population you sell to",
        "identity_breakdown": {"key_only": {"identities": 258},
                               "free_key_durable": {"identities": 258},
                               "email_bound": {"mature_cohort": 3},
                               "oauth_durable": {}, "note": "durable key cohorts"},
    }
    d.update(over)
    return d


def _ctx(f=None, dm=None, r=None):
    return {"funnel": f if f is not None else _funnel(),
            "deadman": dm if dm is not None else _deadman(),
            "retention": r if r is not None else _retention()}


# ── A · press level ─────────────────────────────────────────────────────────
def test_A_fails_when_a_superseded_level_is_quoted_bare():
    assert _v(_M._lane_press_level(_ctx())) == "FAIL"


def test_A_passes_once_the_week_is_no_longer_superseded():
    """★ THE GREEN PATH THAT ARRIVES ON ITS OWN. After 2026-08-24 the complete
    week postdates the correction and the level becomes quotable."""
    f = _funnel(real_external_complete_wk_comparability={
        "superseded_by_correction": False, "quotable_as_trend": True})
    assert _v(_M._lane_press_level(_ctx(f=f))) == "PASS"


def test_A_passes_when_the_sentence_discloses_the_level_not_just_the_delta():
    f = _funnel(press_headline_metric=(
        "DC Hub served 2,100 external AI-agent tool calls in the week of "
        "2026-08-10 — ~80% of those calls were DC Hub's own GitHub Actions and "
        "the figure is superseded by a measurement correction."))
    assert _v(_M._lane_press_level(_ctx(f=f))) == "PASS"


def test_A_is_unmeasured_not_green_when_the_payload_is_missing():
    """★ 'could not measure' must never render as 'fine'."""
    assert _v(_M._lane_press_level({"funnel": None})) == "?"


# ── B · backup health ───────────────────────────────────────────────────────
def test_B_fails_on_a_cancelled_backup():
    assert _v(_M._lane_backup_health(_ctx())) == "FAIL"


def test_B_passes_when_the_backup_is_healthy():
    dm = _deadman(feeds=[
        {"feed": "backup-neon-r2", "status": "success", "age_hours": 2.0,
         "cadence_hours": 30.0, "overdue": False, "reasons": []},
        {"feed": "restore-test", "status": "success", "age_hours": 66.4,
         "cadence_hours": 190.0, "overdue": False, "reasons": []}])
    assert _v(_M._lane_backup_health(_ctx(dm=dm))) == "PASS"


def test_B_fails_when_no_backup_feed_is_tracked_at_all():
    """★ TWO DIFFERENT ABSENCES, TWO DIFFERENT VERDICTS.

    deadman readable but listing no backup feed is a DEFINITE state — there is
    no backup monitoring — and that is a FAIL, not an unknown. An empty or
    unreadable deadman is genuinely UNMEASURED. Collapsing the two would either
    hide a missing backup or cry wolf on a failed read.
    """
    dm = _deadman(feeds=[{"feed": "iso-lmp-pjm", "status": "success",
                          "overdue": False, "reasons": []}])
    assert _v(_M._lane_backup_health(_ctx(dm=dm))) == "FAIL"


def test_B_is_unmeasured_when_deadman_is_empty_or_unreadable():
    assert _v(_M._lane_backup_health({"deadman": {"feeds": []}})) == "?"
    assert _v(_M._lane_backup_health({"deadman": None})) == "?"


# ── C · wall reachability ───────────────────────────────────────────────────
def test_C_fails_when_signals_are_served_but_the_wall_never_fired():
    assert _v(_M._lane_wall_reachability(_ctx())) == "FAIL"


def test_C_passes_once_the_wall_has_actually_produced_a_decision():
    f = _funnel(quota_wall={"enforce": True, "hits_month": 3, "keys_month": 2,
                            "table_exists": True, "lazily_created": True})
    assert _v(_M._lane_wall_reachability(_ctx(f=f))) == "PASS"


def test_C_does_not_fire_when_no_signals_are_being_served():
    """★ FALSE BRANCH. A wall that never fires is only a contradiction when the
    product is simultaneously telling people to upgrade."""
    f = _funnel(real_external_signals_7d=0)
    assert _v(_M._lane_wall_reachability(_ctx(f=f))) == "PASS"


def test_C_does_not_fire_when_enforcement_is_off():
    f = _funnel(quota_wall={"enforce": False, "hits_month": 0,
                            "table_exists": False})
    assert _v(_M._lane_wall_reachability(_ctx(f=f))) == "PASS"


# ── D · conversion honesty ──────────────────────────────────────────────────
def _check_by_id(checks, cid):
    for c in checks:
        if c["id"] == cid:
            return c
    raise AssertionError("check %r not found — fence is pointing at nothing" % cid)


def test_D_fails_when_the_headline_exceeds_the_honest_paid_count():
    assert _v(_M._lane_conversion_honesty(_ctx())) == "FAIL"


def test_D_the_headline_check_itself_fails_not_just_the_lane():
    """★ LANE-VERDICT TESTS CANNOT ISOLATE A CHECK.

    Lane D has two independent failures right now (headline>honest, and
    attribution>bridged). Asserting only `verdict == FAIL` passes even if the
    headline comparison is wired to the WRONG number — a mutation swapping
    paid_total for conversions_30d left the lane FAIL on the attribution half
    alone and the suite stayed green. Pin the individual check.
    """
    checks = _M._lane_conversion_honesty(_ctx())
    assert _check_by_id(checks, "d_headline_not_above_honest")["pass"] is False
    detail = _check_by_id(checks, "d_headline_not_above_honest")["detail"]
    assert "conversions_30d=6" in detail and "honest paid=4" in detail, (
        "the check must compare against paid_total (4), not restate the "
        "headline against itself")


def test_D_headline_check_passes_alone_when_the_filters_agree():
    """The other side of the isolation: honest==headline clears THAT check even
    while the attribution check is still failing."""
    f = _funnel(conversions_30d=4, conversions_attributed_30d=4)
    checks = _M._lane_conversion_honesty(_ctx(f=f))
    assert _check_by_id(checks, "d_headline_not_above_honest")["pass"] is True
    assert _check_by_id(checks, "d_attribution_not_overstated")["pass"] is False


def test_D_passes_when_headline_and_honest_agree():
    f = _funnel(conversions_30d=4, conversions_attributed_30d=1)
    assert _v(_M._lane_conversion_honesty(_ctx(f=f))) == "PASS"


def test_D_fails_on_overstated_attribution_alone():
    """Headline matches honest, but attribution is still asserted above the
    bridge count — each half must fail independently."""
    f = _funnel(conversions_30d=4, conversions_attributed_30d=4)
    assert _v(_M._lane_conversion_honesty(_ctx(f=f))) == "FAIL"


# ── E · identity label ──────────────────────────────────────────────────────
def test_E_fails_when_identified_has_no_email_backed_sibling():
    assert _v(_M._lane_identity_label(_ctx())) == "FAIL"


def test_E_passes_when_an_email_backed_count_is_published_beside_it():
    f = _funnel(distinct_emails_30d=12)
    assert _v(_M._lane_identity_label(_ctx(f=f))) == "PASS"


def test_E_passes_when_no_identified_claim_is_made():
    f = _funnel(keys_by_tier={"free": 114, "paid": 44})
    assert _v(_M._lane_identity_label(_ctx(f=f))) == "PASS"


def test_E_accepts_the_list_shaped_tier_payload():
    """keys_by_tier ships as a dict on one route and a list of rows on another."""
    f = _funnel(keys_by_tier=[{"tier": "identified", "n": 434}],
                distinct_emails_30d=12)
    assert _v(_M._lane_identity_label(_ctx(f=f))) == "PASS"


# ── F · retention population ────────────────────────────────────────────────
def test_F_fails_when_the_key_side_declares_no_externality_filter():
    assert _v(_M._lane_retention_population(_ctx())) == "FAIL"


def test_F_passes_once_the_key_cohorts_declare_their_population():
    r = _retention(identity_breakdown={
        "key_only": {"identities": 258},
        "note": ("these key cohorts are NOT externality filtered — "
                 "is_real_external is applied to agent_cohort only")})
    assert _v(_M._lane_retention_population(_ctx(r=r))) == "PASS"


def test_F_does_not_fire_when_the_primary_metric_is_agent_grain():
    """FALSE BRANCH: an agent-grain primary metric is already covered by
    agent_cohort_note, so the lane must stay quiet."""
    r = _retention(primary_metric="agent_cohort returning share (mcp_calls_identity)")
    assert _v(_M._lane_retention_population(_ctx(r=r))) == "PASS"


def test_F_control_does_not_pass_on_the_wrong_cohort():
    """★ THE VACUOUS-PASS REGRESSION.

    The first draft searched EVERY note for the word 'population' and went
    green off agent_cohort_note. Here the agent side is fully declared and the
    key side is not — the lane must still FAIL. If this ever passes, the lane
    has stopped discriminating between the two sides.
    """
    r = _retention(agent_cohort_note=(
        "CANONICAL GRAIN — population is mcp_calls_identity where "
        "is_real_external AND is_public_ip, the population you sell to"))
    assert _v(_M._lane_retention_population(_ctx(r=r))) == "FAIL"


# ── G · gateway disclosure ──────────────────────────────────────────────────
def test_G_fails_when_a_dominant_gateway_has_no_excluded_variant():
    assert _v(_M._lane_gateway_disclosure(_ctx())) == "FAIL"


def test_G_passes_when_an_excluded_variant_is_published():
    f = _funnel(real_external_calls_7d_excl_top_caller=337)
    assert _v(_M._lane_gateway_disclosure(_ctx(f=f))) == "PASS"


def test_G_does_not_fire_below_the_declared_dominance_threshold():
    """FALSE BRANCH: the rule is about a caller who DOMINATES the number."""
    f = _funnel(top_caller_pct_7d=8.0)
    assert _v(_M._lane_gateway_disclosure(_ctx(f=f))) == "PASS"


# ── G · the nested disclosure form (2026-08-27) ─────────────────────────────
# ★ This lane published a FALSE FAIL for the entire life of the disclosure it
# demands. /api/v1/mcp/funnel really does publish the excluded variant — as
# `demand_net_of_top_caller_7d`, a NESTED OBJECT — and the lane missed it twice
# over: the name is not one of the three `*_excl_*` scalars it grepped, AND
# `_num()` returns None for a dict, so a name match alone would still have
# failed. Live 2026-08-27 the object read {calls: 110, agents: 15,
# top_caller_calls: 885} against a headline of 995 (885 + 110 == 995).
#
# Both halves are pinned below, and both controls are kept: a lane that accepts
# the nested form must still be able to REFUSE an empty one, or this fix just
# converts a false FAIL into a false PASS.
def test_G_passes_when_the_excluded_variant_is_published_in_the_NESTED_form():
    f = _funnel(top_caller_pct_7d=88.9,
                demand_net_of_top_caller_7d={
                    "calls": 110, "agents": 15, "top_caller_calls": 885,
                    "headline_calls": 995, "top_caller_pct": 88.9})
    assert _v(_M._lane_gateway_disclosure(_ctx(f=f))) == "PASS"


def test_G_still_fails_when_the_nested_object_carries_no_remainder():
    """CONTROL for the SHAPE half: the key alone must not satisfy the rule.

    Without this, widening the name list would let an empty object — or any
    object whose remainder fields were renamed away — pass as a disclosure.
    """
    f = _funnel(top_caller_pct_7d=88.9,
                demand_net_of_top_caller_7d={"basis": "...", "top_caller_pct": 88.9})
    assert _v(_M._lane_gateway_disclosure(_ctx(f=f))) == "FAIL"


def test_G_accepts_the_agents_only_remainder():
    f = _funnel(top_caller_pct_7d=88.9,
                demand_net_of_top_caller_7d={"agents": 15})
    assert _v(_M._lane_gateway_disclosure(_ctx(f=f))) == "PASS"


def test_G_still_fails_when_the_nested_key_is_not_an_object():
    """CONTROL: presence of the KEY is not the test — a readable number is."""
    f = _funnel(top_caller_pct_7d=88.9,
                demand_net_of_top_caller_7d="110 calls net of top caller")
    assert _v(_M._lane_gateway_disclosure(_ctx(f=f))) == "FAIL"


# ── H · prose vs data ───────────────────────────────────────────────────────
def test_H_fires_when_a_hardcoded_inflow_claim_meets_a_superseded_population(monkeypatch):
    """The TRUE branch, now driven by a SYNTHETIC page instead of the real one.

    ★ 2026-08-25: this test used to read the live dashboard, which really did
    assert "Inflow is fine; retention is the leak" — so the lane's failing
    branch was exercised by the defect itself. The caption now derives its
    verdict from new vs returning, so the real file no longer trips it and this
    test would have quietly become unfalsifiable: green because the defect is
    gone, indistinguishable from green because the lane stopped looking.

    Inject the sentence instead. The lane must still fire on it.
    """
    monkeypatch.setattr(
        _M, "_repo_text",
        lambda p: 'Inflow is fine; <b>retention</b> is the leak.'
        if p == "static/mcp-dashboard.html" else "")
    assert _v(_M._lane_prose_vs_data(_ctx())) == "FAIL"


def test_H_passes_on_the_real_dashboard_now_that_the_verdict_is_derived():
    """The live file must NOT trip lane H any more — that is the fix landing."""
    assert _v(_M._lane_prose_vs_data(_ctx())) == "PASS"


def test_H_passes_once_the_population_is_no_longer_superseded():
    f = _funnel(real_external_complete_wk_comparability={
        "superseded_by_correction": False})
    assert _v(_M._lane_prose_vs_data(_ctx(f=f))) == "PASS"


def test_H_reads_the_real_dashboard_file():
    """★ NOT BLIND. The lane must be pointed at a file that actually exists.

    ★ UPDATED DELIBERATELY 2026-08-25, exactly as the previous version of this
    test asked to be. It pinned the literal "Inflow is fine" and said: "if it
    was FIXED, this test should be updated deliberately, not left asserting a
    string that no longer exists."

    It was fixed. Measured the week of 2026-08-17: 9 new agents against 8
    returning, with new_agents falling 79 -> 64 -> 9 across three weeks while
    returning held at 5 -> 8 -> 8 and key-reuse climbed every week (33% -> 68%).
    Retention was improving and INFLOW had collapsed, so the sentence asserted
    the opposite of the two numbers printed directly beneath it.

    The caption now DERIVES the verdict from new vs returning instead of
    hardcoding one, so the string this test used to pin is gone on purpose. The
    guard moves with it: assert the sentence is computed, not that a particular
    conclusion is present.
    """
    html = _M._repo_text("static/mcp-dashboard.html")
    assert html, "static/mcp-dashboard.html unreadable — lane H is blind"
    assert "Inflow is fine" not in html, (
        "the hardcoded verdict is back. It asserts a conclusion above the two "
        "numbers that decide it, and it was wrong for at least three weeks")
    assert "_verdict" in html and "binding constraint" in html, (
        "the derived verdict is gone — the caption must compute inflow-vs-"
        "retention from new_agents and returning_agents, not assert either")
    # Assert on the ASSIGNMENTS, not a window around them. Mutation-tested
    # twice: a whole-file check passes because the weekly table also names
    # these fields, and a 700-char window still passes because the caption
    # template interpolates them a few lines below. Only pinning
    # `const _nw = ...acLatest.new_agents...` catches `const _nw = 1;`.
    import re as _re
    for var, field in (("_nw", "new_agents"), ("_rt", "returning_agents")):
        assert _re.search(rf"const\s+{var}\s*=\s*[^;]*acLatest\.{field}", html), (
            f"const {var} no longer derives from acLatest.{field} — the verdict "
            "is a constant again, which is how it stated the opposite of its "
            "own data for three weeks")


# ── shell-level contract ────────────────────────────────────────────────────
def test_every_lane_is_registered():
    ids = {lid for lid, _n, _f in _M._LANES}
    assert ids == {"press_level", "backup_health", "wall_reachability",
                   "conversion_honesty", "identity_label",
                   "retention_population", "gateway_disclosure", "prose_vs_data"}


def test_a_crashing_lane_never_500s_the_shell_and_never_reads_green():
    def boom(_ctx):
        raise RuntimeError("lane exploded")
    saved = _M._LANES[:]
    try:
        _M._LANES[:] = [("boom", "exploding lane", boom)]
        out = _M._run()
        assert out["lanes"][0]["verdict"] == "?"
        assert out["any_unmeasured"] is True
    finally:
        _M._LANES[:] = saved


def test_shell_uses_requests_not_urllib():
    """regression_lint hard-blocks urllib on Railway, and the CF edge 1010s a
    bare urllib User-Agent before it reaches the origin.

    ★ NOT A SUBSTRING SEARCH. The shell's own comment explains why urllib is
    banned, so `"urllib.request" not in src` fails on the documentation that
    exists to prevent the bug. Walk the AST for real imports and calls — same
    class as test_control_divisor_scan_is_not_fooled_by_a_comment.
    """
    import ast
    tree = ast.parse(_SRC)
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            imported.add((n.module or "").split(".")[0])
    assert "requests" in imported, "the shell must fetch with requests"
    assert "urllib" not in imported, "urllib is blocked by regression_lint on Railway"


def test_reads_are_cache_busted():
    """/api/v1/* is CF-cached under Rule #3 with mode=override_origin, which
    ignores no-store. A stale payload would let the shell certify a state that
    no longer exists."""
    assert "_cb=" in _SRC


def test_shell_is_report_only():
    """#54 must not mutate anything — several lanes cover product decisions and
    ops actions that must never be auto-actioned."""
    for bad in ("cur.execute(\"UPDATE", "cur.execute(\"DELETE", "cur.execute(\"INSERT",
                "requests.post(", "requests.put(", "requests.delete("):
        assert bad not in _SRC, f"shell #54 is report-only but contains {bad!r}"


def test_kill_switch_exists():
    assert "PUBLISHED_TRUTH_SHELL_DISABLE" in _SRC


def test_admin_gated():
    assert "_admin_ok()" in _SRC and "unauthorized" in _SRC
