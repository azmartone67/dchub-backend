"""A completed checkout must never lower the tier an account already holds.

THE INCIDENT (2026-09-18, owner). azmartone@gmail.com held `pro`, bought the
$10 one-time credit pack from an agent unlock link, and came out `starter`.
Reproduced from the shipped source, not from the report:

  * the pack is $10 (routes/mcp_conversion_plays.PACK10_PRICE_CENTS = 1000)
  * handle_checkout_completed resolves a plan from the AMOUNT when the
    payment_link id is unmapped, and the $9-Starter band is `8 <= d <= 11`
  * `UPDATE users SET plan = %s ... WHERE email = %s` ran with no comparison
    against what the account already had

The band is not wrong about $9. It is wrong about what ELSE lands in it, and
the plan write had nothing to stop the result.

WHAT IS PROVEN HERE
  1. the rules themselves (routes/_checkout_plan_guard, pure) — every cell of
     the pack x current-plan x subscription table
  2. the wrapper that reads the account and alerts (main._apply_plan_guard),
     EXECUTED out of the shipped main.py by ast, because main.py cannot be
     imported without a database
  3. that handle_checkout_completed actually routes plan_name through it,
     before every write and before the one-time expiry stamp

(3) is a source assertion and is the weakest of the three: it proves the wiring
is present and ordered, not that the 480-line handler has no other path to a
plan write. Both writes it guards are asserted by name below, so a NEW write
added later is what this file would miss.
"""
import ast
import copy
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from routes._checkout_plan_guard import (  # noqa: E402
    is_credit_pack_checkout, resolve_plan_write, pack_price_cents)
from routes.mcp_conversion_plays import (  # noqa: E402
    PACK5_PRICE_CENTS, PACK10_PRICE_CENTS)


def _sess(mode, subtotal, total=None, sid="cs_test_1"):
    return {"mode": mode, "amount_subtotal": subtotal,
            "amount_total": total if total is not None else subtotal, "id": sid}


PACK = _sess("payment", PACK10_PRICE_CENTS)
# r-pack5-tax-fix's lesson, applied to the tier side: Stripe Tax pushes
# amount_total past the price, and a taxed $10 pack ($10.88) is still inside
# the 8..11 Starter band — so matching only amount_total leaves the reported
# defect alive for every taxed sale.
PACK_TAXED = _sess("payment", PACK10_PRICE_CENTS, int(PACK10_PRICE_CENTS * 1.088))
PACK5 = _sess("payment", PACK5_PRICE_CENTS)
STARTER_SUB = _sess("subscription", 900)
PRO_SUB = _sess("subscription", 9900)


class TestTheRules:
    def test_a_pack_is_recognised_gross_or_net_of_tax(self):
        assert is_credit_pack_checkout(PACK)
        assert is_credit_pack_checkout(PACK_TAXED)
        assert is_credit_pack_checkout(PACK5)
        assert pack_price_cents() == {PACK5_PRICE_CENTS, PACK10_PRICE_CENTS}

    def test_a_subscription_is_never_a_pack(self):
        assert not is_credit_pack_checkout(STARTER_SUB)
        assert not is_credit_pack_checkout(PRO_SUB)
        # …not even one that happens to cost a pack price.
        assert not is_credit_pack_checkout(_sess("subscription", PACK10_PRICE_CENTS))

    @pytest.mark.parametrize("session", [PACK, PACK_TAXED, PACK5])
    def test_the_reported_case_a_pro_account_buying_a_pack(self, session):
        plan, tier, note = resolve_plan_write(session, "starter", "starter", "pro")
        assert (plan, tier) == ("pro", "pro"), "the incident, unfixed"
        assert note and "credit pack" in note

    def test_a_pack_does_not_hand_a_new_buyer_a_tier(self):
        # $10 once must not buy Starter's 500 REST + 200 MCP calls/day forever.
        assert resolve_plan_write(PACK, "starter", "starter", "")[:2] == ("free", "free")
        assert resolve_plan_write(PACK, "starter", "starter", None)[:2] == ("free", "free")

    def test_a_pack_leaves_every_tier_exactly_where_it_was(self):
        for held in ("free", "identified", "starter", "developer", "pro",
                     "founding", "team", "enterprise"):
            plan, _tier, _n = resolve_plan_write(PACK, "starter", "starter", held)
            assert plan == held, f"{held} moved on a credit-pack purchase"

    def test_a_cheaper_subscription_cannot_demote_a_paying_account(self):
        plan, tier, note = resolve_plan_write(STARTER_SUB, "starter", "starter", "pro")
        assert (plan, tier) == ("pro", "pro")
        assert note and "refusing to downgrade" in note
        assert resolve_plan_write(STARTER_SUB, "starter", "starter",
                                  "enterprise")[:2] == ("enterprise", "enterprise")
        # founding ranks WITH pro (tier_registry), so a $9 cannot slip past it
        assert resolve_plan_write(STARTER_SUB, "starter", "starter",
                                  "founding")[:2] == ("founding", "pro")

    def test_real_upgrades_and_first_purchases_are_untouched(self):
        for current, bought, tier in [("", "starter", "starter"),
                                      ("free", "starter", "starter"),
                                      ("identified", "starter", "starter"),
                                      ("starter", "developer", "developer"),
                                      ("developer", "pro", "pro"),
                                      ("pro", "enterprise", "enterprise")]:
            got = resolve_plan_write(_sess("subscription", 100), bought, tier, current)
            assert got == (bought, tier, None), f"{current} -> {bought} was altered"

    def test_a_renewal_at_the_same_tier_is_not_treated_as_a_downgrade(self):
        assert resolve_plan_write(PRO_SUB, "pro", "pro", "pro") == ("pro", "pro", None)

    def test_an_unknown_stored_plan_cannot_pin_an_account_above_what_it_paid(self):
        # A garbage value in users.plan ranks as free, so it never wins.
        assert resolve_plan_write(STARTER_SUB, "starter", "starter",
                                  "platinum")[:2] == ("starter", "starter")


# ── the wrapper, executed out of the shipped main.py ──────────────────────
# main.py needs a database at import time; the function under test needs two
# of its globals. Slice it out by ast and give it exactly those two, stubbed —
# a hand-copied wrapper here would go green through its own regression.
_MAIN_AST = ast.parse((REPO / "main.py").read_text(encoding="utf-8"))


def _exec_apply_plan_guard(rows, alerts, prints):
    fn = next((n for n in _MAIN_AST.body
               if isinstance(n, ast.FunctionDef) and n.name == "_apply_plan_guard"), None)
    assert fn is not None, "main.py no longer defines _apply_plan_guard"
    fn = copy.deepcopy(fn)
    fn.decorator_list = []
    ns = {
        "_pg_execute": lambda sql, args=(), fetch=False: (len(rows), list(rows)),
        "send_admin_alert_email": lambda subj, body: alerts.append((subj, body)),
        "print": lambda *a, **k: prints.append(" ".join(str(x) for x in a)),
    }
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "main.py", "exec"), ns)
    return ns["_apply_plan_guard"]


class TestTheShippedWrapper:
    def test_it_reads_the_account_and_holds_a_pro_buyer_of_a_pack(self):
        alerts, prints = [], []
        guard = _exec_apply_plan_guard([("pro",)], alerts, prints)
        plan, tier, note = guard(PACK, "starter", "starter", None, "owner@example.com")
        assert (plan, tier) == ("pro", "pro")
        assert note
        assert alerts, "a held plan must be visible — no admin alert was sent"
        assert "owner@example.com" in alerts[0][1]
        assert any("plan guard" in p for p in prints)

    def test_it_stays_silent_when_the_checkout_stands(self):
        alerts, prints = [], []
        guard = _exec_apply_plan_guard([("free",)], alerts, prints)
        assert guard(STARTER_SUB, "starter", "starter", None,
                     "new@example.com") == ("starter", "starter", None)
        assert alerts == []

    def test_no_account_row_yet_is_a_new_buyer_not_an_error(self):
        alerts, prints = [], []
        guard = _exec_apply_plan_guard([], alerts, prints)
        assert guard(STARTER_SUB, "starter", "starter", None,
                     "nobody@example.com") == ("starter", "starter", None)

    def test_a_broken_lookup_writes_the_unguarded_plan_rather_than_raising(self):
        # Losing the purchase is worse than mis-tiering, and the mis-tier is
        # now the loud case. This must never propagate.
        fn = copy.deepcopy(next(n for n in _MAIN_AST.body
                                if isinstance(n, ast.FunctionDef)
                                and n.name == "_apply_plan_guard"))
        fn.decorator_list = []
        prints = []

        def _boom(*a, **k):
            raise RuntimeError("db down")

        ns = {"_pg_execute": _boom, "send_admin_alert_email": lambda *a: None,
              "print": lambda *a, **k: prints.append(" ".join(str(x) for x in a))}
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "main.py", "exec"), ns)
        assert ns["_apply_plan_guard"](PACK, "starter", "starter", None,
                                       "x@example.com") == ("starter", "starter", None)
        assert any("plan guard failed" in p for p in prints)


# ── the wiring inside handle_checkout_completed ───────────────────────────
class TestTheWiring:
    @staticmethod
    def _handler():
        return next(n for n in _MAIN_AST.body
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "handle_checkout_completed")

    def test_plan_name_is_reassigned_from_the_guard_before_any_plan_write(self):
        fn = self._handler()
        guard_lines = [n.lineno for n in ast.walk(fn)
                       if isinstance(n, ast.Assign)
                       and isinstance(n.value, ast.Call)
                       and getattr(n.value.func, "id", "") == "_apply_plan_guard"
                       and "plan_name" in {getattr(t, "id", "")
                                           for tgt in n.targets
                                           for t in (tgt.elts if isinstance(tgt, ast.Tuple)
                                                     else [tgt])}]
        assert guard_lines, ("handle_checkout_completed does not reassign plan_name "
                             "from _apply_plan_guard — the guard is unwired")
        first_guard = min(guard_lines)
        writes = [n.lineno for n in ast.walk(fn)
                  if isinstance(n, ast.Constant) and isinstance(n.value, str)
                  and "UPDATE users SET plan" in n.value]
        assert writes, "the plan writes this guard protects are gone — re-aim this test"
        assert min(writes) > first_guard, (
            "a plan write runs before the guard: lines %r vs guard at %d"
            % (sorted(writes), first_guard))

    def test_a_held_plan_does_not_collect_the_one_time_expiry_stamp(self):
        fn = self._handler()
        assigns = [n for n in ast.walk(fn)
                   if isinstance(n, ast.Assign)
                   and any(getattr(t, "id", "") == "set_tier_expiry" for t in n.targets)]
        assert assigns, "set_tier_expiry is gone — re-aim this test"
        src = ast.dump(assigns[0])
        assert "_guard_note" in src, (
            "set_tier_expiry does not read _guard_note, so a Pro subscriber who "
            "buys a $10 pack still gets stamped source_plan='pro_onetime'")

    def test_the_rules_are_not_re_typed_inside_main(self):
        # ONE derivation. A second copy of the bands or the pack price in
        # main.py is how the grant path and the plan path came to disagree.
        src = (REPO / "main.py").read_text(encoding="utf-8")
        assert src.count("def _apply_plan_guard(") == 1
        assert "from routes._checkout_plan_guard import resolve_plan_write" in src
