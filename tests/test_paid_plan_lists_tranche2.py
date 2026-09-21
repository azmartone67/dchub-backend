"""The rest of the typed paid-plan lists, and the rank table under three of them.

SECOND SWEEP after #5002 and #5011. `tier_registry.TIERS` marks SEVEN plans
paid: developer, enterprise, founding, pro, research_seed, starter, team.

★ THE ROOT, and the most severe thing in either sweep — `routes/tier_gate.py`.
`_TIER_RANK` had no `TEAM` row, and `require_tier()` does
`_TIER_RANK.get(tier.upper(), 0)`, so a paying Team customer ranked 0 — FREE —
and got a structured 402 from EVERY `require_tier`-decorated route
(deals_routes, find_sites, brain_rag, sites_capacity,
expanded_infrastructure_api, peeringdb_layer, public_endpoints,
paywall_middleware, main.py). It is the third instance of one defect: the file's
own comments record FOUNDING missing (r43-H, denied transactions/market
intel/grid data) and RESEARCH_SEED missing (r43-H, denied the NLR contract).
Both were repaired by typing one more row, which is why there was a third.

★ WHY THREE GATES TYPED AN UPPERCASE SET. `_resolve_caller_tier()` promised
"one of FREE/IDENTIFIED/DEVELOPER/PRO/ENTERPRISE" and does not deliver it: its
JWT branch appends `(_plan.upper(), "cookie:jwt")` straight from the signed
`plan` claim, so TEAM/STARTER/FOUNDING/RESEARCH_SEED all arrive. radar.py,
deal_autopsy.py and grid_transition_radar.py each believed the docstring and
served those customers the teaser. radar.py's set omitted FOUNDING as well,
which TIERS calls Pro-equivalent.

WHAT THESE PIN — every case is generated from `sorted(paid_plan_names())`, so
an eighth paid plan extends this file rather than slipping past it:

  * routes/tier_gate._TIER_RANK — every paid plan outranks FREE, team is
    pro-equivalent, and the derivation reproduces EVERY row it replaced (so it
    cannot quietly re-rank an existing tier while adding the missing one);
  * radar / deal_autopsy / grid_transition_radar — full view, not the teaser;
  * monthly_customer_report / customer_white_glove / customer_portal — the
    `users.plan` roster filters;
  * monetization_master_shell — `mcp_call_log.tier`, a MIXED column, so the
    registry is unioned with the coarse paying words rather than trusted alone;
  * flask_mcp_endpoints._PAID_PLANS / _ENT_PLANS — pinned as DELIBERATELY NOT
    derived. starter/developer sit below 'paid' by design; deriving them would
    be a pricing change.
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import routes.customer_portal as portal                # noqa: E402
import routes.customer_white_glove as glove            # noqa: E402
import routes.deal_autopsy as autopsy                  # noqa: E402
import routes.grid_transition_radar as gtr             # noqa: E402
import routes.monetization_master_shell as mono        # noqa: E402
import routes.monthly_customer_report as report        # noqa: E402
import routes.radar as radar                           # noqa: E402
import routes.tier_gate as tg                          # noqa: E402
from tier_registry import TIERS, paid_plan_names       # noqa: E402

PAID = sorted(paid_plan_names())
UNPAID = sorted({n for n, s in TIERS.items() if not s.get("paid")})


def test_the_parametrisation_cannot_collapse():
    """Without this, an empty canon reduces every case below to zero cases."""
    assert len(PAID) >= 7, PAID
    assert {"team", "research_seed", "starter", "founding"} <= set(PAID), PAID
    assert len(UNPAID) >= 2, UNPAID


# ── the rank table three gates sit on ───────────────────────────────────────
@pytest.mark.parametrize("plan", PAID)
def test_every_paid_plan_outranks_free_in_the_gate(plan):
    rank = tg._TIER_RANK.get(plan.upper(), 0)
    assert rank > tg._TIER_RANK.get("FREE", 0), (
        f"{plan!r} ranks {rank} — the same as FREE — so require_tier() returns "
        f"a 402 to a paying customer on every decorated route")


@pytest.mark.parametrize("plan", ["team", "founding"])
def test_pro_equivalent_plans_clear_a_pro_gate(plan):
    """TIERS gives team and founding api_tier 'pro'; the gate must agree."""
    assert tg._TIER_RANK.get(plan.upper(), 0) >= tg._TIER_RANK["PRO"], plan


def test_research_seed_clears_an_enterprise_gate():
    assert tg._TIER_RANK.get("RESEARCH_SEED", 0) >= tg._TIER_RANK["ENTERPRISE"]


def test_the_derived_table_still_contains_every_row_it_replaced():
    """The derivation may ADD names. It may not change one that was there.

    This is the assertion that makes deriving safe: _ACCESS_LEVEL is a second
    scale, and a wrong mapping would silently re-rank a live tier.
    """
    drift = {k: (old, tg._TIER_RANK.get(k))
             for k, old in tg._FALLBACK_TIER_RANK.items()
             if tg._TIER_RANK.get(k) != old}
    assert not drift, f"derivation changed existing rows: {drift}"


def test_starter_is_still_deliberately_identified_ranked():
    """Phase BBB-3 put STARTER at IDENTIFIED's rank on purpose. Deriving from
    TIERS.rank (starter=2) instead of api_tier would silently promote it."""
    assert tg._TIER_RANK["STARTER"] == tg._TIER_RANK["IDENTIFIED"]
    assert tg._TIER_RANK["STARTER"] < tg._TIER_RANK["DEVELOPER"]


@pytest.mark.parametrize("plan", UNPAID)
def test_unpaid_plans_do_not_outrank_free(plan):
    assert tg._TIER_RANK.get(plan.upper(), 0) <= tg._TIER_RANK.get("IDENTIFIED", 1)


def test_an_unknown_plan_name_still_falls_to_free():
    """Fail closed: the derivation must not invent a rank for a stranger."""
    assert tg._TIER_RANK.get("PLAN_STRIPE_INVENTS_TOMORROW", 0) == 0


# ── the three teaser gates, which read _resolve_caller_tier UPPERCASED ───────
@pytest.mark.parametrize("plan", PAID)
@pytest.mark.parametrize("mod,attr,what", [
    (radar, "PAID", "radar full-vs-teaser"),
    (autopsy, "_PAID", "deal_autopsy paid layer"),
    (gtr, "_PAID", "grid_transition_radar forward thesis"),
])
def test_paid_plans_get_the_full_view_not_the_teaser(mod, attr, what, plan):
    assert plan.upper() in getattr(mod, attr), (
        f"{plan!r} is paid but {what} serves it the teaser; the tier arrives "
        f"UPPERCASED from the signed users.plan claim")


@pytest.mark.parametrize("mod,attr", [
    (radar, "PAID"), (autopsy, "_PAID"), (gtr, "_PAID")])
def test_the_teaser_gates_keep_their_non_plan_gate_words(mod, attr):
    for word in ("ADMIN", "INTERNAL"):
        assert word in getattr(mod, attr), word


@pytest.mark.parametrize("plan", UNPAID)
@pytest.mark.parametrize("mod,attr", [
    (radar, "PAID"), (autopsy, "_PAID"), (gtr, "_PAID")])
def test_unpaid_plans_still_get_the_teaser(mod, attr, plan):
    assert plan.upper() not in getattr(mod, attr), plan


# ── the users.plan roster filters ───────────────────────────────────────────
@pytest.mark.parametrize("plan", PAID)
@pytest.mark.parametrize("mod,attr,what", [
    (report, "PAID_PLANS", "monthly customer report roster"),
    (glove, "PAID_PLANS", "white-glove roster (confirmed payers only)"),
    (portal, "_PAID_PLANS", "customer_portal lifecycle signal"),
])
def test_paid_plans_are_in_every_users_plan_roster(mod, attr, what, plan):
    assert plan in getattr(mod, attr), f"{plan!r} missing from {what}"


@pytest.mark.parametrize("plan", UNPAID)
@pytest.mark.parametrize("mod,attr", [
    (report, "PAID_PLANS"), (glove, "PAID_PLANS"), (portal, "_PAID_PLANS")])
def test_unpaid_plans_are_not_in_a_paid_roster(mod, attr, plan):
    assert plan not in getattr(mod, attr), plan


def test_the_legacy_non_registry_names_survive_the_derivation():
    """Dropping these would demote a real account, so they are kept by name."""
    assert "paid" in glove.PAID_PLANS, "white_glove carried 'paid'"
    assert "pro_annual" in portal._PAID_PLANS, "portal carried 'pro_annual'"


@pytest.mark.parametrize("plan", PAID)
def test_the_portal_sql_copy_cannot_drift_from_the_tuple(plan):
    """Two copies of one list is the defect. The SQL string is generated."""
    assert f"'{plan}'" in portal._PAID_PLANS_SQL, plan
    assert portal._PAID_PLANS_SQL.count("'") == 2 * len(portal._PAID_PLANS)


# ── the MIXED column: registry is necessary, not sufficient ─────────────────
@pytest.mark.parametrize("plan", PAID)
def test_paid_plans_are_not_billed_as_over_threshold_free(plan):
    assert plan in mono._PAID_TIERS, (
        f"tier={plan!r} would be written as an over-threshold FREE user and "
        f"queued for a metering pitch")


def test_the_mixed_column_keeps_its_coarse_paying_words():
    """mcp_call_log.tier is written from body.get('tier') — the caller's word.

    'paid' and 'metered' both mean paying and neither is a registry plan name,
    so the registry alone cannot be the whole answer for this column.
    """
    for word in ("paid", "metered", "admin", "internal"):
        assert word in mono._PAID_TIERS, word
    assert "paid" not in PAID, "'paid' is not a users.plan value"
    assert "metered" not in PAID, "'metered' is not a users.plan value"


@pytest.mark.parametrize("plan", UNPAID)
def test_free_tiers_are_still_billable_as_over_threshold(plan):
    assert plan not in mono._PAID_TIERS, plan


# ── the site that must NOT be derived ───────────────────────────────────────
def _fme(monkeypatch):
    """Import flask_mcp_endpoints, which refuses to load without a DB URL.

    The env var is set through monkeypatch and inside the test, NOT at module
    scope: a module-scope os.environ write leaks into every other test in the
    session. Nothing here connects — the module only reads the value at import
    to decide whether to raise.
    """
    monkeypatch.setenv("NEON_DATABASE_URL",
                       "postgresql://u:p@127.0.0.1:1/db?sslmode=disable")
    import importlib
    return importlib.import_module("flask_mcp_endpoints")


def test_the_node_rank_mapping_is_not_quietly_derived(monkeypatch):
    fme = _fme(monkeypatch)
    """starter/developer sit BELOW 'paid' by design in the Node vocabulary.

    If someone "fixes" these for uniformity, both get promoted to the full
    paid tool set — a pricing change disguised as a refactor. This test fails
    when that happens, and the docstring says why it is intentional.
    """
    mapped = fme._ENT_PLANS | fme._PAID_PLANS
    assert "starter" not in mapped, "starter was promoted to the Node paid tier"
    assert "developer" not in mapped, "developer was promoted to the Node paid tier"
    # ...but the plans that ARE meant to map must stay mapped.
    for plan in ("pro", "founding", "team"):
        assert plan in fme._PAID_PLANS, plan
    for plan in ("enterprise", "research_seed"):
        assert plan in fme._ENT_PLANS, plan


def test_node_mapping_still_normalises_the_granular_names(monkeypatch):
    """Behavioural, not a set comparison: run the real normaliser."""
    fme = _fme(monkeypatch)
    assert fme._node_tier_max(["team"]) == "paid"
    assert fme._node_tier_max(["research_seed"]) == "enterprise"
    assert fme._node_tier_max(["free", "founding"]) == "paid"
    assert fme._node_tier_max(["free"]) not in ("paid", "enterprise")
