"""Brain Ascension #28 wave-1 pins (2026-07-25).

House rule: tests NEVER import main. Everything here imports leaf modules or
reads files directly.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── growth truth: the MRR price maps stay in lock-step ────────────────

def test_price_maps_identical():
    """canonical_funnel and funnel_health each keep a plan→USD map; drift
    between them was how team/founding silently counted $0 for weeks."""
    from canonical_funnel import PLAN_MONTHLY_USD as canon
    from routes.funnel_health import _PLAN_MONTHLY_USD as fh
    assert dict(canon) == dict(fh)


def test_team_and_founding_priced():
    from canonical_funnel import PLAN_MONTHLY_USD as canon
    assert canon.get("team") == 699
    assert canon.get("founding") == 99


def test_mrr_probe_filter_covers_every_paid_plan():
    """Every plan with a nonzero price must appear in the users-probe plan
    filter in funnel_health — a priced plan missing from the IN (...) list
    is invisible to the MRR probe (the team/founding bug)."""
    from canonical_funnel import PLAN_MONTHLY_USD as canon
    src = open(os.path.join(ROOT, "routes", "funnel_health.py"),
               encoding="utf-8").read()
    m = re.search(r"AND plan IN \((.*?)\)", src, re.S)
    assert m, "users-probe plan filter not found"
    filt = m.group(1)
    for plan, usd in canon.items():
        if usd and usd > 0:
            assert f"'{plan}'" in filt, f"paid plan {plan!r} missing from MRR probe filter"


# ── brain deadman: the liveness spine stays registered ────────────────

def test_deadman_registry_covers_brain_spine():
    src = open(os.path.join(ROOT, "tools", "deadman", "watch.py"),
               encoding="utf-8").read()
    for wf in ("cron-heartbeat.yml", "brain-autonomy.yml", "brain-autopilot.yml",
               "brain-verify.yml", "brain-master-tick.yml",
               "brain-model-reachability.yml", "brain-mirror.yml",
               "strategic-briefing-weekly.yml"):
        assert f'"{wf}"' in src, f"{wf} missing from deadman WORKFLOWS registry"


# ── competitor → product wiring ───────────────────────────────────────

def test_planner_has_crawled_gaps_layer():
    from routes.brain_strategic_planner import _read_crawled_gaps  # noqa: F401


def test_universe_models_semianalysis_and_electricity_maps():
    from routes.brain_strategic_planner import _COMPETITOR_UNIVERSE
    names = " ".join(
        e.get("name", "") for cat in _COMPETITOR_UNIVERSE.values()
        if isinstance(cat, list) for e in cat if isinstance(e, dict))
    assert "SemiAnalysis" in names
    assert "Electricity Maps" in names


def test_gaps_endpoint_reads_live_table():
    """Textual check — importing competitor_intelligence pulls a heavyweight
    chain (db init side effects), which tests must never do."""
    src = open(os.path.join(ROOT, "competitor_intelligence.py"),
               encoding="utf-8").read()
    assert "def _crawled_coverage_gaps" in src
    assert "'crawled_gaps': crawled" in src


# ── rag truth ─────────────────────────────────────────────────────────

def test_live_embed_model_tracks_provider(monkeypatch):
    from routes import brain_rag
    monkeypatch.delenv("RAG_EMBED_PROVIDER", raising=False)
    assert brain_rag._live_embed_model() == "mistral-embed"
    monkeypatch.setenv("RAG_EMBED_PROVIDER", "cohere")
    assert brain_rag._live_embed_model() == brain_rag.EMBED_MODEL


def test_every_eval_query_has_anchors():
    from routes.rag_master_shell import _EVAL_QUERIES
    for q in _EVAL_QUERIES:
        assert q.get("anchors"), f"eval query lacks anchor ground truth: {q['q']!r}"
        assert all(a == a.lower() for a in q["anchors"]), \
            f"anchors must be lowercase (matched against lowered text): {q['q']!r}"


# ── shell verdict honesty ─────────────────────────────────────────────

def test_lane_verdict_never_green_by_silence():
    from routes.brain_ascension_master_shell import _check, _lane_verdict
    assert _lane_verdict([_check("a", "a", True, "", critical=True)]) == "PASS"
    assert _lane_verdict([_check("a", "a", None, "", critical=True)]) == "?"
    assert _lane_verdict([_check("a", "a", False, "")]) == "FAIL"
    # non-critical indeterminate does not block green
    assert _lane_verdict([_check("a", "a", True, "", critical=True),
                          _check("b", "b", None, "")]) == "PASS"


def test_wave2_checks_are_real_verification():
    """Wave 2 shipped: the three former placeholder-reds must now be REAL
    checks (registry-derived, table-probing, ANNUAL_OPTIONS-reading) — a
    hardcoded pass would be the same lie in green."""
    src = open(os.path.join(ROOT, "routes", "brain_ascension_master_shell.py"),
               encoding="utf-8").read()
    assert "PROVIDER_COSINE_GATES" in src          # gates verified vs registry
    assert "brain_pr_metric_snapshots" in src      # harness probed via table
    assert "ANNUAL_OPTIONS" in src                 # annual read from registry
    assert "still_broken" in src                   # real brain_fix_outcomes col


# ── wave 2: provider-aware cosine gates ───────────────────────────────

def test_provider_gates_registered_and_helper_strict_on_typo():
    from routes.brain_rag import PROVIDER_COSINE_GATES, cosine_gate
    for prov in ("mistral", "cohere"):
        g = PROVIDER_COSINE_GATES[prov]
        assert set(g) == {"dup_loose", "dup_strict", "related_min", "eval_floor"}
    # mistral scale from the 2026-07-25 live measurement
    m = PROVIDER_COSINE_GATES["mistral"]
    assert m["related_min"] == 0.72 and m["eval_floor"] == 0.70
    assert m["dup_loose"] == 0.90 and m["dup_strict"] == 0.92
    # unknown gate name falls back STRICT (never opens a gate on a typo)
    assert cosine_gate("no_such_gate") == max(m.values())


def test_eval_floors_meet_registered_floor():
    from routes.brain_rag import PROVIDER_COSINE_GATES
    from routes.rag_master_shell import _EVAL_QUERIES, _EVAL_MEAN_FLOOR
    floor = PROVIDER_COSINE_GATES["mistral"]["eval_floor"]
    assert _EVAL_MEAN_FLOOR >= floor
    for q in _EVAL_QUERIES:
        assert float(q["floor"]) >= floor, q["q"]


def test_related_intel_wired_to_registry():
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    assert 'cosine_gate("related_min")' in src
    assert "def _rag_related_intel(query, corpus=None, k=4, min_cosine=None)" in src


def test_dup_gate_defaults_match_registry():
    from routes.brain_rag import PROVIDER_COSINE_GATES
    m = PROVIDER_COSINE_GATES["mistral"]
    fp = open(os.path.join(ROOT, "routes", "brain_feature_proposer.py"),
              encoding="utf-8").read()
    assert f'"{m["dup_loose"]:.2f}"' in fp
    di = open(os.path.join(ROOT, "deal_ingestion_scheduler.py"),
              encoding="utf-8").read()
    assert f"_env_float('DEAL_DUP_COSINE', {m['dup_strict']})" in di


# ── wave 2: metric harness + annual visibility ────────────────────────

def test_metric_harness_module_real():
    src = open(os.path.join(ROOT, "routes", "brain_pr_metric_harness.py"),
               encoding="utf-8").read()
    assert "brain_pr_metric_snapshots" in src
    assert "canonical_funnel" in src              # KPI SoT, never local SQL
    assert "ON CONFLICT (pr_number, phase, metric_key)" in src
    assert not os.path.exists(os.path.join(
        ROOT, "routes", "_proposed_merged_pr_before_after_metric_harness.py"))


def test_annual_options_additive_and_consistent():
    """r-price-collapse (2026-09-05): PRO ANNUAL IS WITHDRAWN.

    This used to pin $1,188/yr and the $1,794 "50% off" promo. Both were
    priced against a $299/mo list. Against the $99 list they are arithmetic
    nonsense — 1188 IS 12 x 99 (a 0% discount) and 1794 is 51% MORE than
    paying monthly — so offering either would punish the buyer for taking it.

    The guard therefore inverts: pro must carry NO annual option. And it adds
    the invariant that outlives any particular price — an annual option that
    exists must beat twelve monthly payments, which is the property that
    silently broke when the monthly price moved and nothing rechecked it.
    """
    from tier_registry import (ANNUAL_OPTIONS, TIER_PRICE_USD_MONTH,
                               as_public_dict)
    assert "pro" not in ANNUAL_OPTIONS, (
        "Pro annual is withdrawn — see the note in tier_registry.ANNUAL_OPTIONS. "
        "To restore it, mint a link that actually beats 12x the monthly price.")
    # ADDITIVE: no annual key leaked into the monthly price map (access/rank
    # surfaces key off that map; test_tier_consistency guards the rest)
    assert not any("annual" in k for k in TIER_PRICE_USD_MONTH)
    d = as_public_dict()
    assert d["annual_options"] is not None
    assert d["tiers"]["pro"].get("annual") in (None, {}), (
        "pro is publishing an annual option that ANNUAL_OPTIONS does not define")

    # ★ The durable invariant: ANY annual option offered must cost LESS than
    #   twelve months at that tier's monthly price. This is what nobody
    #   rechecked when Pro moved 299 -> 99, and it is priced in dollars, not
    #   pinned to a literal, so it survives the next reprice too.
    for tier, opt in ANNUAL_OPTIONS.items():
        monthly = TIER_PRICE_USD_MONTH.get(tier)
        for key in ("annual_usd_year", "annual_promo_usd_year"):
            annual = opt.get(key)
            if annual is None or not monthly:
                continue          # custom/contact pricing has nothing to check
            assert annual < monthly * 12, (
                f"{tier}.{key} is ${annual}/yr against ${monthly}/mo "
                f"(= ${monthly * 12}/yr) — the annual costs MORE than paying "
                f"monthly, so it is a penalty, not an offer")


# ── cron hygiene: the expired one-shots stay deleted ──────────────────

def test_expired_oneshot_workflows_removed():
    wf = os.path.join(ROOT, ".github", "workflows")
    assert not os.path.exists(os.path.join(wf, "announce-global-grid.yml"))
    assert not os.path.exists(os.path.join(wf, "publish-registry-pr.yml"))


# ── wave 3 / shell #29 ────────────────────────────────────────────────

def test_no_hardcoded_live_keys_in_tracked_files():
    """A real customer key + the owner's live enterprise key were committed to
    this PUBLIC repo. Nothing may reintroduce a full key literal."""
    import subprocess
    out = subprocess.run(
        ["git", "grep", "-nIE", r"dchub_(pro|live)_[A-Za-z0-9]{20,}", "--", "."],
        cwd=ROOT, capture_output=True, text=True).stdout.strip()
    def _is_placeholder(line):
        # EVERY key-shaped match on the line must be a placeholder. re.search
        # returns only the FIRST match, so a doc placeholder appearing earlier
        # on the same grep line would whitelist a REAL key after it.
        ms = re.findall(r"dchub_(?:pro|live)_([A-Za-z0-9]{20,})", line)
        if not ms:
            return True
        def _ph(body):
            return (len(set(body.lower())) <= 2
                    or body.lower().startswith(("xxxx", "your", "abc123")))
        return all(_ph(b) for b in ms)
    offenders = [ln for ln in out.splitlines()
                 if ln and not ln.startswith("tests/") and not _is_placeholder(ln)]
    assert not offenders, "hardcoded key literal(s):\n" + "\n".join(offenders[:5])


def test_claim_reuse_restamps_session():
    """The durable-identity carry fix: BOTH reuse branches must re-stamp."""
    src = open(os.path.join(ROOT, "flask_mcp_endpoints.py"), encoding="utf-8").read()
    assert "def _restamp_claim_session" in src
    assert src.count("_restamp_claim_session(") >= 3   # def + 2 call sites
    assert "jsonb_set(" in src and "'{session_id}'" in src


def test_wave3_rag_corpora_registered():
    from routes.brain_rag import CORPORA, PUBLIC_CORPORA, _HYDRATE
    for t in ("press_releases", "announcements", "permitting_intel",
              "construction_permits", "tax_incentives_neon", "capacity_pipeline"):
        assert t in CORPORA, t
        assert t in PUBLIC_CORPORA, t
        assert t in _HYDRATE, f"{t} has no citation hydration"
    # brain_briefs is operator prose — indexed but deliberately NOT public
    assert "brain_briefs" in CORPORA
    assert "brain_briefs" not in PUBLIC_CORPORA


def test_loop_flywheel_shell_honest():
    from routes.loop_flywheel_master_shell import _check, _lane_verdict, _lane_infra
    assert _lane_verdict([_check("a", "a", None, "", critical=True)]) == "?"
    assert _lane_verdict([_check("a", "a", False, "")]) == "FAIL"
    # ★2026-09-03: this asserted the countdown date "2026-10-05" appeared in
    # the lane's output — "must actually count down, not hardcode a pass". The
    # countdown is GONE: it counted down to a Neon cutover that had already
    # executed (2026-07-13), observed nothing but the calendar, and would have
    # gone red permanently from 09-13 with nobody able to clear it.
    # The INTENT — the lane must MEASURE, never hardcode a verdict — survives,
    # and is now checked against what the lane actually observes: the live DSN.
    import os as _os
    _prev = _os.environ.get("DATABASE_URL")
    try:
        _os.environ["DATABASE_URL"] = (
            "postgresql://u:pw@ep-x.westus3.azure.neon.tech/db")
        azure = next(k for k in _lane_infra() if k["id"] == "neon_off_azure")
        _os.environ["DATABASE_URL"] = (
            "postgresql://u:pw@ep-x.c-2.us-west-2.aws.neon.tech/db")
        aws = next(k for k in _lane_infra() if k["id"] == "neon_off_azure")
    finally:
        if _prev is None:
            _os.environ.pop("DATABASE_URL", None)
        else:
            _os.environ["DATABASE_URL"] = _prev
    assert azure["pass"] is False and aws["pass"] is True, \
        "the infra lane hardcodes a verdict instead of observing the live host"


def test_shell29_lanes_have_no_missing_helpers():
    """Shell #29's inventory lane shipped calling _age_days, which was never
    defined in that module — _safe_lane caught it as an honest '?', but the
    lane checked nothing for a full deploy. Exercise every lane offline so a
    NameError can never reach production again."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "lf29", os.path.join(ROOT, "routes", "loop_flywheel_master_shell.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    lanes = [(m._lane_infra, ()), (m._lane_inventory, (None,)),
             (m._lane_ai_doors, (None,)), (m._lane_cron, (None,)),
             (m._lane_identity, (None,)), (m._lane_rag, ()), (m._lane_mcp, ())]
    for fn, args in lanes:
        crashes = [c for c in m._safe_lane(fn, *args) if c["id"] == "lane_crash"]
        assert not crashes, f"{fn.__name__} crashed: {crashes[0]['detail']}"
    assert m._age_days(None) is None
    assert m._age_days("2026-07-24T00:00:00Z") > 0


def test_lane_verdict_never_green_when_nothing_decided():
    """A lane whose checks are ALL indeterminate must render '?', not PASS —
    green-by-silence is the failure mode both shells exist to prevent."""
    for mod in ("routes.loop_flywheel_master_shell",
                "routes.brain_ascension_master_shell"):
        import importlib
        m = importlib.import_module(mod)
        assert m._lane_verdict([m._check("a", "a", None, "")]) == "?", mod
        assert m._lane_verdict([m._check("a", "a", None, ""),
                                m._check("b", "b", None, "")]) == "?", mod
        assert m._lane_verdict([m._check("a", "a", True, "")]) == "PASS", mod
        assert m._lane_verdict([m._check("a", "a", None, ""),
                                m._check("b", "b", True, "")]) == "PASS", mod
        assert m._lane_verdict([m._check("a", "a", False, "")]) == "FAIL", mod


def test_public_rag_corpora_are_gated():
    """Wave-3 corpora join PUBLIC_CORPORA, so unvetted rows would be served
    on the unauthenticated /api/v1/rag/search. Publish/promotion gates must
    stay in the corpus WHERE."""
    from routes.brain_rag import CORPORA
    assert "coalesce(t.published, FALSE) IS TRUE" in CORPORA["press_releases"]["where"]
    assert "t.row_status = 'published'" in CORPORA["permitting_intel"]["where"]


def test_claim_restamp_records_bind_time():
    """The reconcile sweep's temporal guard keys off session_bound_at; without
    it the re-stamp would back-fill pre-claim anonymous calls onto the key and
    inflate the carry metric the fix is measured by."""
    src = open(os.path.join(ROOT, "flask_mcp_endpoints.py"), encoding="utf-8").read()
    assert "session_bound_at" in src
    assert "_bound_at" in src
    assert "AND k.created_at <= l.timestamp" not in src   # old unguarded form
    assert "AND k.created_at <= l2.timestamp" not in src


def test_qa_sweep_ships_no_credentials():
    src = open(os.path.join(ROOT, "qa-sweep.sh"), encoding="utf-8").read()
    assert "DCHub2026" not in src            # shared account password
    assert "theterrills@gmail.com" not in src
    assert "SKIP: qa-sweep needs" in src     # fail-fast guard, not fabricated FAILs
