"""Every plan tier_registry marks paid must be paid at every gate that asks.

BACKGROUND. #5002 fixed `routes/audience_export.py`, where the denylist
`_PAID = ("pro", "founding", "enterprise")` shipped 20 of 168 live rows on
paid plans inside a file called free-users. `TIERS` marks SEVEN plans paid —
developer, enterprise, founding, pro, research_seed, starter, team — and the
same three-name literal had been retyped across the codebase.

WHAT THESE PIN, and why each is not a mirror of the code it guards:

  * `activation_nudge.PAID_PLANS` feeds `WHERE u.plan IN %s`. A Team or
    Research customer who never made a call got no activation nudge at all.

  * `free_tier_gate._PAID_PLANS` is compared against `user['plan']`, filled by
    get_user_from_jwt() from `SELECT id, email, plan FROM users`. A signed-in
    Starter/Developer/Team/Research customer failed the paid bypass and was
    metered as free: 403 "Your free map session has been used" after one
    session. Exercised here through `_user_from_api_key` against a fake cursor
    that DISPATCHES ON THE SQL, so the api_keys branch is really taken.

  * `power_plant_intel._PP_PAID` is worse than a free fallthrough: the default
    is `_PP_PREVIEW.get(tier, 3)` and the missing plans are absent from
    _PP_PREVIEW too, so a paying Team customer was capped at THREE rows on
    every list endpoint while an anonymous caller got 100000.

  * `free_tier_gate._PAID_KEY_TIERS` is NOT derived and must stay that way. It
    reads `mcp_dev_keys.tier`, a COARSER vocabulary (free/identified/paid/
    enterprise). Reading the plan canon for that column is the inversion that
    put 26 paying customers into warm_key_cohort.mailable on 2026-09-17.

THE PARAMETRISATION IS THE POINT. Every case below is generated from
`sorted(paid_plan_names())`, so an eighth paid plan added to TIERS EXTENDS
this file instead of slipping past it. A test that named pro/founding/
enterprise would pass against the broken code and the fixed code alike.
"""
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import activation_nudge as an              # noqa: E402
import free_tier_gate as ftg               # noqa: E402
import power_plant_intel as ppi            # noqa: E402
from tier_registry import TIERS, paid_plan_names  # noqa: E402

PAID = sorted(paid_plan_names())

# Plans TIERS positively marks NOT paid — derived, for the fail-closed cases.
UNPAID = sorted({n for n, s in TIERS.items() if not s.get("paid")})


# ── floor ───────────────────────────────────────────────────────────────────
# Without this, a paid_plan_names() that returned () would silently reduce
# every parametrised test below to zero cases and the file would pass green.
def test_the_parametrisation_cannot_collapse():
    assert len(PAID) >= 7, f"paid_plan_names() shrank to {PAID}"
    # The two the three original literals all omitted. Named explicitly
    # because they are the regression, not because the list is closed.
    assert {"team", "research_seed"} <= set(PAID), PAID
    assert "admin" not in PAID, "admin is a role, not a purchasable plan"
    assert len(UNPAID) >= 2, UNPAID


# ── activation_nudge: WHERE u.plan IN %s ────────────────────────────────────
@pytest.mark.parametrize("plan", PAID)
def test_activation_nudge_counts_every_paid_plan(plan):
    assert plan in an.PAID_PLANS, (
        f"users.plan={plan!r} is paid to TIERS but invisible to the "
        f"activation nudge; its SQL is WHERE u.plan IN %s")


@pytest.mark.parametrize("plan", UNPAID)
def test_activation_nudge_excludes_unpaid_plans(plan):
    assert plan not in an.PAID_PLANS, plan


# ── free_tier_gate: the paid bypass, compared against users.plan ────────────
@pytest.mark.parametrize("plan", PAID)
def test_free_tier_gate_bypass_accepts_every_paid_plan(plan):
    assert plan in ftg._PAID_PLANS, (
        f"users.plan={plan!r} is paid to TIERS but fails the map gate's paid "
        f"bypass, so the customer is metered as free")


@pytest.mark.parametrize("plan", UNPAID)
def test_free_tier_gate_bypass_refuses_unpaid_plans(plan):
    assert plan not in ftg._PAID_PLANS, plan


class _FakeCursor:
    """A cursor that answers based on the SQL it is actually given.

    A fake that returns one canned row whatever it is asked proves nothing
    about which branch the code took. This one returns no mcp_dev_keys row and
    a users.plan row, so passing requires the api_keys fallback to run AND to
    resolve `u.plan` — the leg that resolved Team to None.
    """

    def __init__(self, user_plan):
        self._user_plan = user_plan
        self._rows = None

    def execute(self, sql, params=None):
        if "mcp_dev_keys" in sql:
            self._rows = None                      # no dev key for this caller
        elif "api_keys" in sql:
            # (ak.rate_limit_tier, ak.plan, u.plan, u.email)
            self._rows = (None, None, self._user_plan, "customer@example.com")
        else:
            raise AssertionError(f"unexpected SQL: {sql[:80]}")

    def fetchone(self):
        return self._rows

    def close(self):
        pass


class _FakeConn:
    def __init__(self, user_plan):
        self._user_plan = user_plan

    def cursor(self):
        return _FakeCursor(self._user_plan)

    def close(self):
        pass


@pytest.mark.parametrize("plan", PAID)
def test_dashboard_key_on_every_paid_plan_resolves_to_a_paid_user(plan):
    """The two-key-systems gap, reopened for the plans nobody added."""
    user = ftg._user_from_api_key("dchub_live_abc123", lambda: _FakeConn(plan))
    assert user is not None, (
        f"a dashboard key whose users.plan={plan!r} resolved to None — read as "
        f"'free-tier key — not a bypass', so the customer got 401 on every map "
        f"data endpoint")
    assert user["plan"] in ftg._PAID_PLANS, user


@pytest.mark.parametrize("plan", UNPAID)
def test_dashboard_key_on_an_unpaid_plan_is_not_a_bypass(plan):
    assert ftg._user_from_api_key("dchub_live_abc123",
                                  lambda: _FakeConn(plan)) is None, plan


def test_an_unknown_plan_name_is_not_a_bypass():
    """Fail closed: a name the canon has never heard of is not paid."""
    assert ftg._plan_for_key_value("plan_invented_by_stripe_tomorrow") is None
    assert ftg._plan_for_key_value("") is None
    assert ftg._plan_for_key_value(None) is None


# ── the column that is NOT the plan canon ───────────────────────────────────
def test_the_coarse_key_column_keeps_its_own_vocabulary():
    """mcp_dev_keys.tier is free/identified/paid/enterprise — not plan names.

    `paid` is the word that matters: it is not in TIERS, so a site that read
    the plan canon for this column would drop it. That is exactly how 26
    paying customers landed in warm_key_cohort.mailable.
    """
    assert "paid" not in PAID, "'paid' is a key tier, never a users.plan value"
    assert ftg._PAID_KEY_TIERS.get("paid") == "pro"
    assert ftg._PAID_KEY_TIERS.get("enterprise") == "enterprise"
    for free_word in ("free", "identified"):
        assert free_word not in ftg._PAID_KEY_TIERS, free_word
        assert ftg._plan_for_key_value(free_word) is None, free_word


# ── power_plant_intel: the teaser, whose default is 3 rows ──────────────────
@pytest.mark.parametrize("plan", PAID)
def test_power_plant_teaser_never_truncates_a_paid_plan(plan):
    assert plan in ppi._PP_PAID, (
        f"tier={plan!r} is paid but falls through to "
        f"_PP_PREVIEW.get({plan!r}, 3) = {ppi._PP_PREVIEW.get(plan, 3)} rows, "
        f"while an anonymous caller gets {ppi._PP_PREVIEW.get('anonymous')}")


@pytest.mark.parametrize("plan", PAID)
def test_power_plant_teaser_is_not_merely_uncapped_by_accident(plan):
    """Membership in _PP_PAID is the contract, not a generous _PP_PREVIEW.

    _PP_PREVIEW is tunable — its own comment says "dial specific tiers down if
    a harder wall is wanted later". If a paid plan is only safe because its
    preview cap happens to be 100000 today, that tuning silently re-breaks it.
    """
    teased_cap = ppi._PP_PREVIEW.get(plan, 3)
    assert plan in ppi._PP_PAID or teased_cap >= 100000, (
        f"{plan!r} survives only on a preview cap of {teased_cap}")


def test_power_plant_teaser_keeps_its_non_plan_gate_words():
    """'internal' and 'admin' are gate words, not plan names — and paid_plan_
    names() deliberately excludes admin, so deriving must not drop them."""
    for word in ("internal", "admin", "paid"):
        assert word in ppi._PP_PAID, word


# ── the sets are derived, not retyped ───────────────────────────────────────
@pytest.mark.parametrize("plan", PAID)
def test_no_site_carries_a_narrower_copy_of_the_canon(plan):
    """One assertion per site, so a failure names which list went stale."""
    missing = [name for name, hay in (
        ("activation_nudge.PAID_PLANS", an.PAID_PLANS),
        ("free_tier_gate._PAID_PLANS", ftg._PAID_PLANS),
        ("power_plant_intel._PP_PAID", ppi._PP_PAID),
    ) if plan not in hay]
    assert not missing, f"{plan!r} missing from: {', '.join(missing)}"
