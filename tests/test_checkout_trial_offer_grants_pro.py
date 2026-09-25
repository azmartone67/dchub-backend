"""A $0 Pro-trial checkout must resolve to Pro, not to the free amount band.

The Pro 7-day trial Payment Link bills $0 at checkout and carries metadata
offer=pro_trial_7d (no `plan`). handle_checkout_completed resolves the plan
from metadata.plan, then the plink map, then amount bands — and $0 matches no
band, so the trialist was provisioned 'free'. handle_subscription_created only
acts on status 'active', so nothing else granted Pro until day 8.
"""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN = ROOT / "main.py"
SRC = MAIN.read_text(encoding="utf-8")
TREE = ast.parse(SRC)


def _helper():
    ns = {}
    nodes = [n for n in TREE.body
             if (isinstance(n, ast.FunctionDef) and n.name == "_plan_from_checkout_offer")
             or (isinstance(n, ast.Assign)
                 and any(getattr(t, "id", "") == "_CHECKOUT_OFFER_PLAN" for t in n.targets))]
    assert len(nodes) == 2, "helper or its map missing from main.py"
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(MAIN), "exec"), ns)  # noqa: S102
    return ns["_plan_from_checkout_offer"]


def _trial_session(**over):
    s = {"mode": "subscription", "amount_total": 0,
         "payment_status": "no_payment_required",
         "metadata": {"offer": "pro_trial_7d"}}
    s.update(over)
    return s


def test_trial_offer_resolves_pro_monthly():
    assert _helper()(_trial_session()) == "pro_monthly"


def test_offer_on_one_time_payment_is_not_a_plan():
    assert _helper()(_trial_session(mode="payment")) == ""


def test_unknown_or_missing_offer_resolves_nothing():
    f = _helper()
    assert f(_trial_session(metadata={"offer": "something_else"})) == ""
    assert f(_trial_session(metadata={})) == ""
    assert f(_trial_session(metadata=None)) == ""
    assert f(None) == ""


def test_resolved_key_is_one_the_checkout_handler_grants():
    # The helper's output must be a key of handle_checkout_completed's
    # plan_tier_map mapping to pro — otherwise it silently falls through.
    fn = next(n for n in TREE.body
              if isinstance(n, ast.FunctionDef) and n.name == "handle_checkout_completed")
    tier_map = next(n for n in ast.walk(fn) if isinstance(n, ast.Assign)
                    and getattr(n.targets[0], "id", "") == "plan_tier_map")
    mapping = ast.literal_eval(tier_map.value)
    assert mapping[_helper()(_trial_session())] == ("pro", "pro")


def test_checkout_handler_consults_offer_before_amount_bands():
    fn = next(n for n in TREE.body
              if isinstance(n, ast.FunctionDef) and n.name == "handle_checkout_completed")
    assign = next(n for n in ast.walk(fn) if isinstance(n, ast.Assign)
                  and getattr(n.targets[0], "id", "") == "plan_from_metadata")
    calls = [c.func.id for c in ast.walk(assign.value)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)]
    assert "_plan_from_checkout_offer" in calls


# r-trial-plink (2026-09-24): the live trial link carries no offer metadata,
# so the Payment Link id alone must resolve Pro.
def _live_trial_session(**over):
    from routes._stripe_links import PRO_TRIAL_PAYMENT_LINK_ID
    s = {"mode": "subscription", "amount_total": 0, "metadata": {},
         "payment_link": PRO_TRIAL_PAYMENT_LINK_ID}
    s.update(over)
    return s


def test_trial_payment_link_without_metadata_resolves_pro_monthly():
    assert _helper()(_live_trial_session()) == "pro_monthly"


def test_trial_payment_link_on_one_time_payment_is_not_a_plan():
    assert _helper()(_live_trial_session(mode="payment")) == ""


def test_other_payment_links_resolve_nothing_here():
    f = _helper()
    assert f(_live_trial_session(payment_link="plink_1UCTZKJ9ey2ATcQlByJCXN3W")) == ""
    assert f(_live_trial_session(payment_link="")) == ""
    # an offer KEY smuggled in as a payment_link value is not a link
    assert f(_live_trial_session(payment_link="pro_trial_7d")) == ""


def test_main_keys_the_canonical_trial_link_id():
    from routes._stripe_links import PRO_TRIAL_PAYMENT_LINK_ID
    offer_map = next(n.value for n in TREE.body if isinstance(n, ast.Assign)
                     and any(getattr(t, "id", "") == "_CHECKOUT_OFFER_PLAN" for t in n.targets))
    assert ast.literal_eval(offer_map).get(PRO_TRIAL_PAYMENT_LINK_ID) == "pro_monthly"
