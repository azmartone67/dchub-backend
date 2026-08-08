"""r-milestone (2026-08-07): guard the editorial desk's NUMBERS lane.

Operator directive: "we just passed 4 million transactions on our /ai page!
that is a press release ... new product rollouts is a story, new numbers."
brain_capability_radar already owns the rollouts; media_milestones owns the
numbers. These tests pin the four ways this lane could ship dead or dishonest:

  1. IT CAN BE SELECTED. Every headline the registry renders clears the desk's
     leads_with_number() gate and scores inside the desk's real band (bar 8,
     cap 45). This is the operator-lead bug of PR #2356 (seeded 0.90 against a
     bar of 8 — produced, never selectable) and the silent-drop bug the desk
     documents twice; a lead that cannot win a slot is a lead that does not
     exist. ★ Two of these headlines were verified to FAIL the pre-#2358 gate.
  2. IT FIRES ONCE, AND ONLY UPWARD. A crossing already on record does not
     re-announce; a metric that slipped back announces nothing; a first
     sighting with no record SEEDS instead of announcing a level it cannot
     prove is new.
  3. IT NEVER CALLS REQUESTS "USERS". label_is_honest is an EXECUTED guard at
     render time, not a lint — a registry edit that describes a count of
     requests with a people-noun yields NO lead.
  4. IT IS REGISTERED EVERYWHERE. The partially-registered kind (pattern in one
     map, missing from another) is this codebase's signature silent failure.

CI-SAFETY: the unit-tests job installs ONLY pytest, so we never import
routes.media_editorial / routes.media_milestones at module scope (Flask,
psycopg2). The pure pieces are AST-extracted and exec'd in an isolated
namespace — same pattern as tests/test_media_editorial_classify.py.
"""
import ast
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MM = os.path.join(ROOT, "routes", "media_milestones.py")
ME = os.path.join(ROOT, "routes", "media_editorial.py")
QD = os.path.join(ROOT, "routes", "linkedin_quad_daily.py")
CE = os.path.join(ROOT, "routes", "content_enqueue.py")


def _extract(path, names, seed=None):
    """Exec just the named top-level Assign/FunctionDef nodes in an isolated
    namespace seeded with stdlib only."""
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    pieces = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in names
                for t in node.targets):
            pieces.append(ast.get_source_segment(src, node))
        elif isinstance(node, ast.FunctionDef) and node.name in names:
            pieces.append(ast.get_source_segment(src, node))
    ns = dict(seed or {})
    exec(compile("\n\n".join(pieces), path, "exec"), ns)
    return ns


class _Log:
    def warning(self, *a, **k): pass
    def info(self, *a, **k): pass
    def debug(self, *a, **k): pass


_MS = _extract(MM, {"_MILESTONES", "_MILESTONE_SCORE_CAP", "_baseline_for",
                    "_MAJOR_BUCKET_BONUS", "_BANNED_LABELS",
                    "label_is_honest", "_canon_num", "milestone_crossing",
                    "render_milestone_lead", "_spec"},
               seed={"re": re, "logger": _Log()})
_DESK = _extract(ME, {"_YEAR_ONLY", "_HAS_METRIC", "leads_with_number",
                      "_KIND_PATTERNS", "_classify_kind", "_KIND_TO_TOPIC"},
                 seed={"re": re})

_MILESTONES = _MS["_MILESTONES"]
label_is_honest = _MS["label_is_honest"]
_canon_num = _MS["_canon_num"]
milestone_crossing = _MS["milestone_crossing"]
render_milestone_lead = _MS["render_milestone_lead"]
leads_with_number = _DESK["leads_with_number"]

# The live canonical values on the day this shipped, so the rendered copy under
# test is the copy production would actually emit.
_CTX = {"ai_requests_total": 4_004_292, "ai_requests_external": 312_504,
        "facilities": 16_900, "deals": 1_700, "tools": 82, "countries": 170}

_DESK_BAR = 8.0          # media_editorial._NEWSWORTHY_MIN
_DESK_CAP = 45.0         # this lane's cap; a marquee deal (~80) still wins


def _spec(key):
    return next(s for s in _MILESTONES if s["key"] == key)


def _lead_for(key, threshold=None):
    s = _spec(key)
    if threshold is None:
        threshold = (_CTX[key] // s["step"]) * s["step"]
    return render_milestone_lead(s, threshold, _CTX)


# ── 1 · it can actually be SELECTED ──────────────────────────────────

def test_every_milestone_headline_clears_the_desk_number_gate():
    """rank_data_events DROPS any lead whose headline fails this gate, and it
    drops them silently. A milestone that cannot pass is never published."""
    for s in _MILESTONES:
        lead = _lead_for(s["key"])
        assert lead, f"{s['key']} rendered nothing"
        head = lead["headline_number"]
        assert leads_with_number(head), \
            f"{s['key']} headline would be silently dropped by the desk: {head!r}"


def test_the_gate_still_rejects_a_numberless_lead():
    """The `\\+?` widening must not turn the number gate into a rubber stamp."""
    for bad in ("DC Hub is the authority on data-center infrastructure",
                "A milestone was reached this week",
                "In 2026 the buildout accelerated"):
        assert not leads_with_number(bad), f"gate went vacuous on: {bad!r}"


def test_the_four_million_requests_headline_is_the_operator_directive():
    lead = _lead_for("ai_requests_total")
    assert lead["threshold"] == 4_000_000, lead
    assert "4,000,000" in lead["headline_number"], lead["headline_number"]
    assert leads_with_number(lead["headline_number"])


def test_scores_sit_on_the_desks_real_scale():
    """Above the bar so it can be chosen, under the cap so a genuine $19B deal
    (~80) or an agent-demand story (~66) still outranks it."""
    for s in _MILESTONES:
        lead = _lead_for(s["key"])
        assert _DESK_BAR < lead["score"] <= _DESK_CAP, \
            f"{s['key']} scored {lead['score']} — off the desk's scale"


def test_a_major_crossing_scores_higher_but_stays_capped():
    five_m = _lead_for("ai_requests_total", 5_000_000)
    four_m = _lead_for("ai_requests_total", 4_000_000)
    assert five_m["score"] > four_m["score"]
    assert five_m["score"] <= _DESK_CAP


# ── 2 · fires ONCE, and only upward ──────────────────────────────────

def test_a_crossing_already_on_record_does_not_re_announce():
    s = _spec("ai_requests_total")
    assert milestone_crossing(s, 4_004_292, 4_000_000) is None
    assert milestone_crossing(s, 4_999_999, 4_000_000) is None


def test_the_next_million_does_announce():
    s = _spec("ai_requests_total")
    res = milestone_crossing(s, 5_000_001, 4_000_000)
    assert res and res["action"] == "announce" and res["threshold"] == 5_000_000


def test_the_documented_3m_seed_is_what_lets_4m_fire():
    """The 3M crossing was announced by hand on 2026-07-27, so 3,000,000 is a
    KNOWN last-announced level — not a guess. Without it, first sighting would
    seed at 4M and the operator's milestone would never be announced."""
    s = _spec("ai_requests_total")
    assert s["seed_value"] == 3_000_000
    res = milestone_crossing(s, 4_004_292, None)
    assert res and res["action"] == "announce" and res["threshold"] == 4_000_000


def test_first_sighting_without_a_documented_seed_seeds_instead_of_announcing():
    """We cannot prove the current level is new, so we remember it and stay
    silent. Announcing here is how a lane claims a months-old number as news."""
    for key in ("facilities", "deals", "tools"):
        s = _spec(key)
        assert s.get("seed_value") is None, f"{key} must not carry a seed"
        res = milestone_crossing(s, _CTX[key], None)
        assert res and res["action"] == "seed", res


def test_a_metric_that_slipped_back_announces_nothing():
    """POSITIVE-RESULTS MANDATE: only an increase is a milestone."""
    s = _spec("facilities")
    assert milestone_crossing(s, 16_400, 17_000) is None


def test_facilities_crossing_17k_is_the_next_real_event():
    s = _spec("facilities")
    res = milestone_crossing(s, 17_000, 16_900)
    assert res and res["action"] == "announce" and res["threshold"] == 17_000
    lead = _lead_for("facilities", 17_000)
    assert "17,000" in lead["headline_number"]


def test_junk_and_missing_values_never_announce():
    s = _spec("facilities")
    for v in (None, 0, -5, "", "n/a", float("nan")):
        res = milestone_crossing(s, v, 1_000)
        assert res is None or res["action"] != "announce", (v, res)


def test_each_crossing_gets_its_own_dedup_key():
    a = _lead_for("ai_requests_total", 4_000_000)["dedup_key"]
    b = _lead_for("ai_requests_total", 5_000_000)["dedup_key"]
    assert a != b and a.startswith("platform_milestone:")
    assert "@4000000" in a


# ── 3 · honest labels, enforced at RENDER time ───────────────────────

def test_no_milestone_copy_calls_a_count_of_requests_people():
    for s in _MILESTONES:
        lead = _lead_for(s["key"])
        blob = " ".join(str(lead.get(k) or "")
                        for k in ("headline_number", "trend", "so_what"))
        assert label_is_honest(blob), f"{s['key']} copy used a people-noun: {blob}"


def test_the_requests_lead_names_the_external_subset():
    """4M is ALL sources. The external AI-platform subset must be named in the
    same lead, or the copy reads as if 4M third parties called us."""
    lead = _lead_for("ai_requests_total")
    assert "312,504" in lead["trend"], lead["trend"]
    assert "external" in lead["trend"].lower()


def test_the_honest_label_guard_actually_rejects():
    """MUST-FAIL: not a lint but an executed render-time guard. A registry that
    describes the counter as users yields NO lead at all."""
    bad = dict(_spec("ai_requests_total"))
    bad["headline"] = lambda t, c: f"DC Hub has now served {t:,}+ total requests to users"
    assert render_milestone_lead(bad, 4_000_000, _CTX) is None
    for word in ("visitors", "customers", "people", "subscribers"):
        assert not label_is_honest(f"4,000,000+ requests from {word}")


def test_a_renderer_that_raises_yields_no_partial_lead():
    boom = dict(_spec("deals"))

    def _raise(t, c):
        raise RuntimeError("boom")

    boom["headline"] = _raise
    assert render_milestone_lead(boom, 1_800, _CTX) is None


# ── 4 · registered in every map the kind must appear in ──────────────

def test_the_kind_classifies_back_to_itself():
    """The bandit attributes reach by classifying posted text back to a kind.
    Unregistered -> everything lands in 'other' and the learner is blind."""
    classify = _DESK["_classify_kind"]
    for s in _MILESTONES:
        head = _lead_for(s["key"])["headline_number"]
        assert classify(head) == "platform_milestone", \
            f"{s['key']} misclassified as {classify(head)}: {head!r}"


def test_the_kind_is_mapped_for_the_topic_tuner():
    assert _DESK["_KIND_TO_TOPIC"].get("platform_milestone"), \
        "unmapped kind gets no learned weight and silently loses selection"


def test_the_kind_is_mapped_for_the_x_bluesky_drumbeat():
    src = open(CE, encoding="utf-8").read()
    tree = ast.parse(src)
    mapping = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_LEAD_KIND_TO_SLOT_TOPIC"
                for t in node.targets):
            mapping = ast.literal_eval(node.value)
    assert mapping, "_LEAD_KIND_TO_SLOT_TOPIC not found"
    assert mapping.get("platform_milestone"), \
        ("unmapped kind falls back to dcpi_mover — a '4,000,000 requests' lead "
         "would be handed to the DCPI composer")


def test_the_crossing_is_retired_only_on_a_successful_publish():
    """Read-only leads + mark-on-publish. Without the quad hook the lane
    re-leads with the same crossing every single day."""
    src = open(QD, encoding="utf-8").read()
    assert "mark_milestone_announced" in src, \
        "the quad never retires a milestone — it would repeat forever"
    assert 'get("kind") == "platform_milestone"' in src
    # the call must sit behind the success check, like the radar's
    i = src.index("mark_milestone_announced")
    assert 'result or {}).get("ok")' in src[max(0, i - 700):i], \
        "milestone retired without checking the post actually succeeded"


def test_a_shared_metric_name_is_not_a_shared_baseline():
    """★ Measured live 2026-08-07, first production run of this lane.

    data_milestone_snapshots.facility_coverage = 24,118 — a raw COUNT(*) of
    discovered_facilities RECORDS. canon publishes 16,900+, the deduped
    citation-safe FLOOR. Same metric name, different quantity, 43% apart.
    Folding the radar's number in as this lane's baseline silently retires
    every facilities crossing up to 24,000 — including the 17,000 one this lane
    was built for. Only ai_requests_total genuinely shares a quantity (both are
    SUM(total_requests) over ai_cumulative), and only it may share a baseline."""
    _bl = _MS["_baseline_for"]
    RADAR_LIVE = {"requests_served_total": 3_000_000.0,
                  "facility_coverage": 24_118.0}

    fac = _spec("facilities")
    assert fac.get("shares_baseline") is False
    assert _bl(fac, {}, RADAR_LIVE) is None, \
        "the radar's raw record count leaked in as a facilities baseline"
    # ...so a first run SEEDS at the canonical floor, and 17,000 stays reachable
    seeded = milestone_crossing(fac, _CTX["facilities"], None)
    assert seeded["action"] == "seed" and seeded["threshold"] == 16_000
    fired = milestone_crossing(fac, 17_000, 16_000)
    assert fired["action"] == "announce" and fired["threshold"] == 17_000

    req = _spec("ai_requests_total")
    assert req.get("shares_baseline") is True
    assert _bl(req, {}, RADAR_LIVE) == 3_000_000.0, \
        "the requests lane MUST share the radar's baseline — same quantity"


def test_only_a_shared_quantity_writes_back_to_the_radar_ledger():
    """The write-back mirrors the read: pushing a deduped floor into a
    raw-count baseline would corrupt the radar's own bucket math."""
    src = open(MM, encoding="utf-8").read()
    i = src.index("INSERT INTO data_milestone_snapshots")
    guard = src[max(0, i - 500):i]
    assert 'spec.get("shares_baseline")' in guard, \
        "the radar write-back is not gated on a shared quantity"


def test_the_lane_de_conflicts_with_the_capability_radar():
    """Four radar rows are numeric milestones over the same metrics. One
    crossing must not produce two leads."""
    me = open(ME, encoding="utf-8").read()
    assert "platform_milestone_leads" in me, "lane not wired into the desk"
    assert 'radar_key' in me and 'data_milestone' in me, \
        "no same-run de-confliction against the radar's milestone leads"
    shared = {s["key"]: s.get("radar_key") for s in _MILESTONES}
    assert shared["ai_requests_total"] == "requests_served_total"
    assert shared["facilities"] == "facility_coverage"


# ── canon parsing: the floored published phrases ─────────────────────

def test_canon_phrases_parse_to_their_floor():
    assert _canon_num("16,900+") == 16900
    assert _canon_num("1,700+") == 1700
    assert _canon_num("170+") == 170
    assert _canon_num(82) == 82
    for junk in (None, "", "n/a", "+", 0, -3, True, False, [], {}):
        assert _canon_num(junk) is None, junk
