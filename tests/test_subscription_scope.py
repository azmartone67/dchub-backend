"""routes/subscription_scope (backend#5555, P1): which subscription an
account's access rests on. Handler-level Postgres coverage lives in
tests/test_lifecycle_writers_tier_sql.py (the #5555 block)."""
import pytest

from routes import subscription_scope as ss


class _List:
    def __init__(self, data):
        self.data = data


def _fake(customers_by_email, subs_by_customer, boom=False):
    class Customer:
        @staticmethod
        def list(email=None, limit=None):
            if boom:
                raise RuntimeError("stripe down")
            return _List([{"id": c} for c in customers_by_email.get(email, [])])

    class Subscription:
        @staticmethod
        def list(customer=None, status=None, limit=None):
            return _List([{"id": i, "status": st, "created": cr}
                          for i, st, cr in subs_by_customer.get(customer, [])])

    return type("S", (), {"Customer": Customer, "Subscription": Subscription})


def test_finds_another_live_subscription_and_skips_the_ended_one():
    f = _fake({"a@x.com": ["cus_1", "cus_2"]},
              {"cus_1": [("sub_1", "active", 100)], "cus_2": [("sub_2", "active", 200)]})
    got = ss.other_live_subscription(["a@x.com"], "sub_2", stripe_mod=f)
    assert got[:2] == ("cus_1", "active") and got[2]["id"] == "sub_1"


def test_newest_live_wins_across_addresses_and_statuses():
    f = _fake({"a@x.com": ["cus_1"], "b@x.com": ["cus_3"]},
              {"cus_1": [("sub_1", "past_due", 100)], "cus_3": [("sub_3", "trialing", 300)]})
    assert ss.other_live_subscription(["a@x.com", "b@x.com"], "sub_x", stripe_mod=f)[:2] == ("cus_3", "trialing")


@pytest.mark.parametrize("status", ["canceled", "incomplete", "incomplete_expired", "unpaid", "paused"])
def test_a_subscription_that_does_not_pay_is_not_live(status):
    f = _fake({"a@x.com": ["cus_1"]}, {"cus_1": [("sub_1", status, 100)]})
    assert ss.other_live_subscription(["a@x.com"], "sub_x", stripe_mod=f) is None


def test_no_addresses_means_none_and_duplicates_are_asked_once():
    calls = []

    class Customer:
        @staticmethod
        def list(email=None, limit=None):
            calls.append(email)
            return _List([])

    f = type("S", (), {"Customer": Customer, "Subscription": None})
    assert ss.other_live_subscription([], "s", stripe_mod=f) is None
    assert ss.other_live_subscription(["A@x.com", "a@x.com", "", None], "s", stripe_mod=f) is None
    assert calls == ["A@x.com"]


def test_a_stripe_failure_raises_for_the_caller_to_decide():
    with pytest.raises(RuntimeError):
        ss.other_live_subscription(["a@x.com"], "s", stripe_mod=_fake({}, {}, boom=True))


def test_customer_has_live_subscription():
    f = _fake({}, {"cus_1": [("s", "active", 1)], "cus_2": [("s", "canceled", 1)]})
    assert ss.customer_has_live_subscription("cus_1", stripe_mod=f) is True
    assert ss.customer_has_live_subscription("cus_2", stripe_mod=f) is False
    assert ss.customer_has_live_subscription("", stripe_mod=f) is False



# ── owner follow-ups (2026-09-25) ──────────────────────────────────────────

def _sub(price_id, cents):
    return {"items": {"data": [{"price": {"id": price_id, "unit_amount": cents}}]}}


@pytest.mark.parametrize("price_id,cents,want", [
    (ss.PRO_PRICE_ID, 9900, ("pro", "pro")),              # Pro's own price is Pro,
    (ss.FOUNDING_PRICE_ID, 9900, ("founding", "pro")),    # founding's is founding,
    ("price_other_99", 9900, ("founding", "pro")),        # an unknown $99 keeps the old band
    ("p", 4900, ("developer", "developer")),
    ("p", 70000, ("enterprise", "enterprise")),
    ("p", 1234, (None, None)),
])
def test_plan_from_subscription(price_id, cents, want):
    assert ss.plan_from_subscription(_sub(price_id, cents))[:2] == want


def test_repoint_statements_move_keys_before_the_users_row():
    stmts, plan = ss.repoint_statements("cus_old", ("cus_new", "active", _sub("p", 4900)))
    assert plan == "developer"
    assert [q.split()[1] for q, _ in stmts] == ["api_keys", "mcp_dev_keys", "users"]
    mcp = stmts[1][0]
    assert "SET tier = 'paid' WHERE tier = 'enterprise'" in mcp       # down only
    q, params = stmts[-1]
    assert "role = CASE WHEN role = 'admin' THEN role ELSE %s END" in q
    assert params == ("cus_new", "active", "developer", "developer", "cus_old")


def test_repoint_statements_for_an_enterprise_plan_touch_no_mcp_key():
    stmts, plan = ss.repoint_statements("cus_old", ("cus_new", "active", _sub("p", 70000)))
    assert plan == "enterprise"
    assert [q.split()[1] for q, _ in stmts] == ["api_keys", "users"]


def test_repoint_statements_with_an_unknown_price_move_only_the_customer():
    stmts, plan = ss.repoint_statements("cus_old", ("cus_new", "past_due", _sub("p", 1234)))
    assert plan is None and len(stmts) == 1
    assert "plan" not in stmts[0][0].split("WHERE")[0]


def test_main_resolver_delegates_here():
    import ast, pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "_resolve_plan_from_subscription")
    assert "plan_from_subscription(subscription)" in ast.get_source_segment(src, fn)


def test_the_webhook_answers_503_and_forgets_the_event_on_an_unavailable_check():
    import ast, pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    hook = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "stripe_webhook")
    body = ast.get_source_segment(src, hook)
    assert body.count("type(_e_sub).__name__ == 'CheckUnavailable'") == 2   # updated + deleted
    tail = body[body.index("if _cancel_check_unavailable:"):]
    assert "_stripe_event_forget(_evt_id)" in tail and "503" in tail.split("\n\n")[0]
    forget = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_stripe_event_forget")
    fsrc = ast.get_source_segment(src, forget)
    assert "DELETE FROM stripe_webhook_events WHERE event_id = %s" in fsrc


def test_stripe_event_forget_deletes_the_record():
    import ast, pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == "_stripe_event_forget")
    calls = []
    ns = {"_pg_execute": lambda q, p=(), fetch=False: calls.append((q, p)) or (1, []),
          "print": lambda *a, **k: None}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "main.py", "exec"), ns)
    ns["_stripe_event_forget"]("evt_1")
    ns["_stripe_event_forget"]("")
    assert calls == [("DELETE FROM stripe_webhook_events WHERE event_id = %s", ("evt_1",))]
